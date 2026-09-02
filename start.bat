@echo off
title Landis+Gyr Live Meter Service
cd /d "%~dp0"

set "PYTHON_EXE="

:: Locate python.exe inside the WinPython directory
for /r ".\WPy64-313150" %%F in (python.exe) do (
    if exist "%%F" (
        set "PYTHON_EXE=%%F"
        goto :found
    )
)

:found
if not defined PYTHON_EXE (
    echo [ERROR] python.exe was not found inside WPy64-313150!
    pause
    exit /b
)

echo Found Python at: "%PYTHON_EXE%"
echo.
echo Installing dependencies...
"%PYTHON_EXE%" -m pip install --find-links=.\win_packages flask gurux-dlms gurux-net requests
echo.
echo Starting Meter Poller and Web Server...
"%PYTHON_EXE%" app.py

pause