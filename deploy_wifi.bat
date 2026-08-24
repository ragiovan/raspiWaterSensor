@echo off
REM Pushes waterSensor.py to the Pico W over Wi-Fi as main.py, using WebREPL.
REM One-time setup required before this will work -- see README_DEPLOY.md.
setlocal

set /p PICO_IP="Pico IP address (shown on boot / in its Telegram startup message): "
set /p WEBREPL_PW="WebREPL password: "

echo.
echo Pushing waterSensor.py to %PICO_IP% as main.py...
python tools\webrepl_cli.py -p %WEBREPL_PW% waterSensor.py %PICO_IP%:main.py

if errorlevel 1 (
    echo.
    echo Deploy FAILED. Check the IP address, password, and that the Pico is
    echo powered on and connected to Wi-Fi.
) else (
    echo.
    echo Done. Reboot the Pico ^(power cycle, or press RESET^) to run the new code.
)
pause
