@echo off
REM ONE-TIME step, run once with the Pico plugged in via USB. Enables WebREPL
REM so deploy_wifi.bat can push future code updates without USB again.
python tools\setup_webrepl.py
pause
