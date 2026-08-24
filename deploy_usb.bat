@echo off
REM Pushes waterSensor.py to the Pico W over USB as main.py (auto-runs on every boot)
REM and resets the board so it starts running immediately.
echo Deploying waterSensor.py to Pico over USB...
mpremote connect auto fs cp waterSensor.py :main.py + reset
if errorlevel 1 (
    echo.
    echo Deploy FAILED. Is the Pico plugged in via USB? Close Thonny or any other
    echo program that might be holding the serial port open, then try again.
) else (
    echo.
    echo Done. Pico is rebooting and will run main.py automatically from now on,
    echo even without USB connected, as long as it has power and Wi-Fi.
)
pause
