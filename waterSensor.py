import network
import socket
import time
import machine
from machine import Pin, time_pulse_us
import gc

# --- Hardware Configuration ---
# TRIG: GP0 (Output)
# ECHO: GP1 (Input via voltage divider if receiving 5V logic)
# VBUS: 5V power to JSN-SR04T
TRIG_PIN = 0
ECHO_PIN = 1
LED_PIN = "LED"

# --- Tank & Calibration Constants ---
# JSN-SR04T has a physical blind spot of ~20-25cm
TANK_DEPTH_CM = 150.0       # Total depth of tank
SENSOR_OFFSET_CM = 25.0     # Distance from sensor face to 100% full mark
NUM_SAMPLES = 5             # Moving average sample count

# --- Wi-Fi Credentials ---
SSID = "YOUR_WIFI_SSID"
PASSWORD = "YOUR_WIFI_PASSWORD"

# --- Initialize Pins ---
trig = Pin(TRIG_PIN, Pin.OUT)
echo = Pin(ECHO_PIN, Pin.IN)
led = Pin(LED_PIN, Pin.OUT)

trig.value(0)
time.sleep_ms(50)

# --- State Variables ---
history = []
last_water_level = None
last_reading_time = None
fill_rate_cm_per_hr = 0.0

def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(SSID, PASSWORD)
        print("Connecting to Wi-Fi...", end="")
        timeout = 20
        while not wlan.isconnected() and timeout > 0:
            led.toggle()
            time.sleep(0.5)
            timeout -= 1
            print(".", end="")
        print()
    if wlan.isconnected():
        led.value(1)
        ip = wlan.ifconfig()[0]
        print(f"Connected! IP: {ip}")
        return ip
    else:
        led.value(0)
        print("Wi-Fi connection failed.")
        return None

def get_raw_distance():
    """Trigger the JSN-SR04T and calculate distance in cm."""
    trig.value(0)
    time.sleep_us(5)
    trig.value(1)
    time.sleep_us(10)
    trig.value(0)
    
    try:
        # 30000us timeout (~5m max range)
        duration = time_pulse_us(echo, 1, 30000)
        if duration > 0:
            # Speed of sound: 343 m/s -> 0.0343 cm/us
            distance = (duration * 0.0343) / 2
            return distance
    except OSError:
        pass
    return None

def get_filtered_reading():
    """Take multiple samples, discard outliers, and return rolling average."""
    readings = []
    for _ in range(NUM_SAMPLES):
        d = get_raw_distance()
        if d is not None and 20.0 <= d <= 450.0:  # Respect sensor physical limits
            readings.append(d)
        time.sleep_ms(60)
        
    if not readings:
        return None
        
    readings.sort()
    # Trim min/max if enough samples exist
    if len(readings) >= 4:
        readings = readings[1:-1]
        
    avg_distance = sum(readings) / len(readings)
    
    # Calculate water level relative to tank geometry
    water_level = TANK_DEPTH_CM - (avg_distance - SENSOR_OFFSET_CM)
    water_level = max(0.0, min(TANK_DEPTH_CM, water_level))
    percentage = (water_level / TANK_DEPTH_CM) * 100.0
    
    return avg_distance, water_level, percentage

def update_metrics():
    global last_water_level, last_reading_time, fill_rate_cm_per_hr
    reading = get_filtered_reading()
    if reading is None:
        return None
        
    raw_dist, water_level, percentage = reading
    current_time = time.time()
    
    if last_water_level is not None and last_reading_time is not None:
        time_diff_hours = (current_time - last_reading_time) / 3600.0
        if time_diff_hours > 0:
            level_diff = water_level - last_water_level
            fill_rate_cm_per_hr = level_diff / time_diff_hours
            
    last_water_level = water_level
    last_reading_time = current_time
    
    return {
        "raw_distance": round(raw_dist, 1),
        "water_level": round(water_level, 1),
        "percentage": round(percentage, 1),
        "fill_rate": round(fill_rate_cm_per_hr, 2)
    }

def serve_dashboard():
    ip = connect_wifi()
    if not ip:
        return

    addr = socket.getaddrinfo('0.0.0.0', 80)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(2)
    print(f"Dashboard listening on http://{ip}:80")

    while True:
        try:
            gc.collect()
            cl, addr = s.accept()
            req = cl.recv(1024).decode('utf-8')
            
            data = update_metrics()
            
            if data is None:
                html = "<html><body><h2>Sensor reading error or out of range</h2></body></html>"
            else:
                pct = data['percentage']
                color = "#27ae60" if pct > 30 else ("#f39c12" if pct > 15 else "#e74c3c")
                
                # Estimate time remaining if filling
                est_time = "N/A"
                if data['fill_rate'] > 0.05:
                    hrs_remaining = (TANK_DEPTH_CM - data['water_level']) / data['fill_rate']
                    est_time = f"{hrs_remaining:.1f} hrs until full"
                elif data['fill_rate'] < -0.05:
                    hrs_remaining = data['water_level'] / abs(data['fill_rate'])
                    est_time = f"{hrs_remaining:.1f} hrs until empty"

                html = f"""<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="30">
    <title>Water Level Monitor</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #121212; color: #fff; text-align: center; margin-top: 40px; }}
        .card {{ background: #1e1e1e; max-width: 380px; margin: auto; padding: 24px; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.5); }}
        .meter {{ background: #333; border-radius: 8px; height: 28px; width: 100%; overflow: hidden; margin: 20px 0; }}
        .fill {{ height: 100%; width: {pct}%; background: {color}; transition: width 0.5s; }}
        .stat {{ display: flex; justify-content: space-between; margin: 10px 0; font-size: 15px; color: #bbb; }}
        .val {{ color: #fff; font-weight: bold; }}
    </style>
</head>
<body>
    <div class="card">
        <h2>Tank Water Level</h2>
        <div class="meter"><div class="fill"></div></div>
        <h1 style="color: {color}; margin: 0 0 20px 0;">{pct}%</h1>
        <div class="stat"><span>Water Level:</span><span class="val">{data['water_level']} cm</span></div>
        <div class="stat"><span>Distance to Sensor:</span><span class="val">{data['raw_distance']} cm</span></div>
        <div class="stat"><span>Flow Rate:</span><span class="val">{data['fill_rate']} cm/hr</span></div>
        <div class="stat"><span>Estimate:</span><span class="val">{est_time}</span></div>
    </div>
</body>
</html>"""

            cl.send('HTTP/1.0 200 OK\r\nContent-type: text/html\r\n\r\n')
            cl.send(html)
            cl.close()
            
        except Exception as e:
            print("Server loop error:", e)
            try:
                cl.close()
            except:
                pass

if __name__ == "__main__":
    serve_dashboard()