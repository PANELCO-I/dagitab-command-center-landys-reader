import socket
import threading
import time
from flask import Flask, jsonify, render_template
from gurux_dlms.GXReplyData import GXReplyData
from gurux_dlms.enums import InterfaceType, Authentication, Security
from gurux_dlms.objects.GXDLMSRegister import GXDLMSRegister
from gurux_dlms.secure.GXDLMSSecureClient import GXDLMSSecureClient

app = Flask(__name__)

# --- CONFIGURATION ---
FEEDERS = [
    {"name": "FEEDER 22/4A", "ip": "172.16.26.185", "port": 4059},
    {"name": "FEEDER 4", "ip": "192.168.0.4", "port": 80},
    {"name": "FEEDER 5", "ip": "192.168.0.5", "port": 4059},
    {"name": "FEEDER 6", "ip": "192.168.0.6", "port": 80},
    {"name": "FEEDER 7", "ip": "192.168.0.7", "port": 4059},
]

OBIS_CODES = {
    "voltage_l1": "1-1:32.7.0.255",
    "voltage_l2": "1-1:52.7.0.255",
    "current_l1": "1-1:31.7.0.255",
    "current_l2": "1-1:51.7.0.255",
    "frequency": "1-1:14.7.0.255",
    "active_energy": "1-1:1.8.0.255",
}

live_readings = {
    feeder["name"]: {
        "ip": feeder["ip"],
        "status": "Initializing...",
        "last_updated": "-",
        "data": {},
    }
    for feeder in FEEDERS
}


def log(name: str, msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] [{name}] {msg}", flush=True)


def udp_probe(ip: str, port: int, name: str):
    """
    NOTE: DLMS wrapper (IEC 62056-47) spec is TCP-only. Gurux client here
    is TCP-based too - there is no real DLMS-over-UDP path. This just sends
    an empty UDP packet and listens briefly, so we can rule UDP in/out fast.
    A reply here would NOT mean DLMS works over UDP, only that something is
    listening on that UDP port.
    """
    try:
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.settimeout(2.0)
        u.sendto(b"", (ip, port))
        log(name, f"UDP probe sent to {ip}:{port}, waiting for any reply...")
        try:
            data, addr = u.recvfrom(1024)
            log(name, f"UDP reply received ({len(data)} bytes) from {addr} - something is listening on UDP.")
        except socket.timeout:
            log(name, "UDP probe: no reply (expected if device is TCP-only, which DLMS wrapper normally is).")
    except Exception as err:
        log(name, f"UDP probe error: {err}")
    finally:
        u.close()


def read_single_meter(ip: str, port: int, name: str = "") -> dict:
    """Connects to a Landis+Gyr meter via DLMS TCP Wrapper."""
    client = GXDLMSSecureClient()
    client.interfaceType = InterfaceType.WRAPPER
    client.clientAddress = 16
    client.serverAddress = 1
    client.authentication = Authentication.NONE
    client.security = Security.NONE

    results = {}

    # --- Step 1: TCP connect ---
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3.0)
        log(name, f"Connecting TCP to {ip}:{port} ...")
        s.connect((ip, port))
        log(name, "TCP connect OK.")
    except socket.timeout:
        log(name, "FAIL @ TCP connect: timed out (port not reachable / blocked / wrong IP-port)")
        udp_probe(ip, port, name)
        return {"status": "Offline (TCP connect timeout)", "data": {}}
    except ConnectionRefusedError as err:
        log(name, f"FAIL @ TCP connect: connection refused ({err}) - nothing listening on this port")
        return {"status": "Offline (connection refused)", "data": {}}
    except OSError as err:
        log(name, f"FAIL @ TCP connect: OS error ({err}) - check IP/route/network")
        return {"status": f"Offline (connect error: {err})", "data": {}}
    except Exception as err:
        log(name, f"FAIL @ TCP connect: unexpected error ({err})")
        return {"status": f"Offline (connect error: {err})", "data": {}}

    # --- Step 2: AARQ / AARE handshake ---
    try:
        aarq_requests = client.aarqRequest()
        log(name, f"Built {len(aarq_requests)} AARQ frame(s).")
        for i, req in enumerate(aarq_requests):
            log(name, f"Sending AARQ frame {i+1}/{len(aarq_requests)} ...")
            s.sendall(bytes(req))
            log(name, "Waiting for AARE response...")
            resp = s.recv(1024)
            if not resp:
                log(name, "FAIL @ AARE: connection closed by remote with no data")
                s.close()
                return {"status": "Offline (closed during handshake)", "data": {}}
            log(name, f"Received {len(resp)} bytes for AARE.")
            client.parseAare(bytearray(resp))
        log(name, "Association established (AARE parsed OK).")
    except socket.timeout:
        log(name, "FAIL @ AARQ/AARE: timed out waiting for reply - meter not responding to this client "
                   "(possible session already held by another client, e.g. MAP110)")
        s.close()
        return {"status": "Offline (AARE timeout - session busy?)", "data": {}}
    except Exception as err:
        log(name, f"FAIL @ AARQ/AARE: {err}")
        s.close()
        return {"status": f"Offline (handshake error: {err})", "data": {}}

    # --- Step 3: read each OBIS register ---
    for key, obis_code in OBIS_CODES.items():
        try:
            reg = GXDLMSRegister(obis_code)
            read_reqs = client.read(reg, 2)

            for req in read_reqs:
                s.sendall(bytes(req))
                resp_bytes = s.recv(1024)
                if not resp_bytes:
                    raise RuntimeError("connection closed during read")

                reply = GXReplyData()
                client.getData(bytearray(resp_bytes), reply)
                client.updateValue(reg, 2, reply.value)

            val = reg.value
            results[key] = round(float(val), 2) if val is not None else "N/A"
        except socket.timeout:
            log(name, f"FAIL @ read '{key}' ({obis_code}): timed out")
            results[key] = "Err"
        except Exception as err:
            log(name, f"FAIL @ read '{key}' ({obis_code}): {err}")
            results[key] = "Err"

    s.close()
    log(name, "Read cycle complete.")
    return {"status": "Online", "data": results}


def background_poller():
    """Polls meters continuously in a background thread every 2 seconds."""
    global live_readings
    while True:
        timestamp = time.strftime("%H:%M:%S")
        for feeder in FEEDERS:
            name = feeder["name"]
            res = read_single_meter(feeder["ip"], feeder["port"], name)

            live_readings[name]["status"] = res["status"]
            live_readings[name]["last_updated"] = timestamp
            if res["data"]:
                live_readings[name]["data"] = res["data"]

        time.sleep(2)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/live")
def get_live():
    return jsonify(live_readings)


if __name__ == "__main__":
    t = threading.Thread(target=background_poller, daemon=True)
    t.start()

    print("Serving dashboard at http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)