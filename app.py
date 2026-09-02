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
# Update the IPs/ports below to match your meters.
FEEDERS = [
    {"name": "FEEDER 22/4A", "ip": "192.168.0.22", "port": 4059},
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


def read_single_meter(ip: str, port: int) -> dict:
    """Connects to a Landis+Gyr meter via DLMS TCP Wrapper."""
    client = GXDLMSSecureClient()
    client.interfaceType = InterfaceType.WRAPPER
    client.clientAddress = 16
    client.serverAddress = 1
    client.authentication = Authentication.NONE
    client.security = Security.NONE

    results = {}

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(3.0)
            s.connect((ip, port))

            aarq_requests = client.aarqRequest()
            for req in aarq_requests:
                s.sendall(bytes(req))
                resp = s.recv(1024)
                client.parseAare(bytearray(resp))

            for key, obis_code in OBIS_CODES.items():
                try:
                    reg = GXDLMSRegister(obis_code)
                    read_reqs = client.read(reg, 2)

                    for req in read_reqs:
                        s.sendall(bytes(req))
                        resp_bytes = s.recv(1024)

                        reply = GXReplyData()
                        client.getData(bytearray(resp_bytes), reply)
                        client.updateValue(reg, 2, reply.value)

                    val = reg.value
                    results[key] = round(float(val), 2) if val is not None else "N/A"
                except Exception:
                    results[key] = "Err"

            return {"status": "Online", "data": results}

    except Exception as err:
        return {"status": f"Offline ({str(err)})", "data": {}}


def background_poller():
    """Polls meters continuously in a background thread every 2 seconds."""
    global live_readings
    while True:
        timestamp = time.strftime("%H:%M:%S")
        for feeder in FEEDERS:
            name = feeder["name"]
            res = read_single_meter(feeder["ip"], feeder["port"])

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