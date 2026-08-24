import network
import socket
import time
import machine
from machine import Pin, time_pulse_us
import gc
import urequests
import ntptime

# --- Hardware Configuration ---
TRIG_PIN = 0
ECHO_PIN = 1
LED_PIN = "LED"

# --- Tank & Calibration Constants ---
# Calibrated against the actual bucket (14.25in = 36.2cm tall): empty reads
# ~37.1cm from the sensor, so SENSOR_OFFSET_CM is that minus the bucket depth.
# Note the sensor's ~20cm minimum range means anything above ~47% full is
# already unreadable directly -- see take_trend_sample's extrapolation.
TANK_DEPTH_CM = 36.2         # Bucket depth, sensor to 100%-full mark
SENSOR_OFFSET_CM = 0.9       # Distance from sensor face to 100% full mark
NUM_SAMPLES = 5              # Moving average sample count

# --- Alert Thresholds ---
HIGH_WATER_ALERT_PCT = 90.0  # Alert when tank exceeds 90%
LOW_WATER_ALERT_PCT = 15.0   # Alert when tank drops below 15%
ALERT_COOLDOWN_SEC = 3600    # Minimum 1 hour between repeating same alert

# The bucket's 3/4-full mark sits closer than the sensor's ~20cm blind spot,
# so it can never be read directly -- it has to be estimated from the trend
# instead (see take_trend_sample). THREE_QUARTER_RESET_PCT is hysteresis: the
# alert re-arms only after level estimate drops back below it, so it doesn't
# resend every 5 hours while sitting above 75%.
THREE_QUARTER_ALERT_PCT = 75.0
THREE_QUARTER_RESET_PCT = 65.0

# --- Trend Sampling ---
# The trend/fill-rate average and the periodic Telegram update are driven by
# this timer, not by dashboard page loads, so they stay accurate whether or
# not anyone is looking at the page.
TREND_INTERVAL_SEC = 5 * 3600  # take one trend sample / send one status update every 5 hours
HISTORY_MAX = 48               # ~10 days of 5-hour samples

# --- Quiet Hours ---
# No Telegram messages 10pm-8am Eastern. The Pico has no DST logic, so this
# offset needs to be flipped by hand: -4 for EDT (Mar-Nov), -5 for EST (winter).
TIMEZONE_OFFSET_HOURS = -4
QUIET_HOURS_START = 22  # 10pm
QUIET_HOURS_END = 8     # 8am

# --- Wi-Fi Credentials ---
SSID = "OpenWrt2.4G"
PASSWORD = "Hack895."

# --- Telegram / CallMeBot Configuration ---
# Option A: Standard Telegram Bot (unused unless USE_CALLMEBOT is False and
# these are set to a real bot token/chat ID from @BotFather)
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN"      # e.g., "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"          # e.g., "987654321"

# Option B: CallMeBot Telegram API, activated by messaging the CallMeBot bot
# from this Telegram username. No API key needed for this account.
USE_CALLMEBOT = True
CALLMEBOT_USER = "@rickgio"

# --- Initialize Pins ---
trig = Pin(TRIG_PIN, Pin.OUT)
echo = Pin(ECHO_PIN, Pin.IN)
led = Pin(LED_PIN, Pin.OUT)

trig.value(0)
time.sleep_ms(50)

# --- State Variables ---
history = []              # (timestamp, water_level_cm) trend samples, one every TREND_INTERVAL_SEC
last_trend_time = 0       # time.time() of the last trend sample; 0 forces an immediate first sample
fill_rate_cm_per_hr = 0.0
last_high_alert_time = 0
last_low_alert_time = 0
last_valid_level = None      # most recent level that came from a real (non-estimated) reading
last_valid_time = None       # time.time() of that reading
three_quarter_alert_sent = False

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

def sync_time():
    """Sets the Pico's RTC from NTP (UTC) so quiet-hours checks are meaningful."""
    try:
        ntptime.settime()
        print("Time synced via NTP")
    except Exception as e:
        print("NTP sync failed:", e)

def in_quiet_hours():
    local_hour = time.localtime(time.time() + TIMEZONE_OFFSET_HOURS * 3600)[3]
    return local_hour >= QUIET_HOURS_START or local_hour < QUIET_HOURS_END

def send_telegram_alert(message):
    """Sends a Telegram notification with error handling. Suppressed 10pm-8am."""
    if in_quiet_hours():
        print("Quiet hours active, suppressing Telegram message:", message)
        return
    try:
        if USE_CALLMEBOT:
            # CallMeBot Gateway (username-based, no API key for this account)
            encoded_msg = message.replace(' ', '+').replace('\n', '%0A')
            url = f"https://api.callmebot.com/text.php?user={CALLMEBOT_USER}&text={encoded_msg}"
            res = urequests.get(url)
            res.close()
        else:
            # Direct Telegram Bot API
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown"
            }
            res = urequests.post(url, json=payload)
            res.close()
        print("Telegram alert sent successfully.")
    except Exception as e:
        print("Failed to send Telegram alert:", e)

def get_raw_distance():
    trig.value(0)
    time.sleep_us(5)
    trig.value(1)
    time.sleep_us(10)
    trig.value(0)
    
    try:
        duration = time_pulse_us(echo, 1, 30000)
        if duration > 0:
            return (duration * 0.0343) / 2
    except OSError:
        pass
    return None

def get_filtered_reading():
    readings = []
    for _ in range(NUM_SAMPLES):
        d = get_raw_distance()
        if d is not None and 20.0 <= d <= 450.0:
            readings.append(d)
        time.sleep_ms(60)
        
    if not readings:
        return None
        
    readings.sort()
    if len(readings) >= 4:
        readings = readings[1:-1]
        
    avg_distance = sum(readings) / len(readings)
    water_level = TANK_DEPTH_CM - (avg_distance - SENSOR_OFFSET_CM)
    water_level = max(0.0, min(TANK_DEPTH_CM, water_level))
    percentage = (water_level / TANK_DEPTH_CM) * 100.0
    
    return avg_distance, water_level, percentage

def check_thresholds_and_alert(percentage, level, rate):
    global last_high_alert_time, last_low_alert_time
    now = time.time()
    
    # High Water Warning
    if percentage >= HIGH_WATER_ALERT_PCT:
        if (now - last_high_alert_time) > ALERT_COOLDOWN_SEC:
            msg = f"⚠️ *HIGH WATER WARNING*\nLevel: {percentage:.1f}% ({level:.1f} cm)\nRate: {rate:+.1f} cm/hr"
            send_telegram_alert(msg)
            last_high_alert_time = now
            
    # Low Water Warning
    elif percentage <= LOW_WATER_ALERT_PCT:
        if (now - last_low_alert_time) > ALERT_COOLDOWN_SEC:
            msg = f"⚠️ *LOW WATER WARNING*\nLevel: {percentage:.1f}% ({level:.1f} cm)\nRate: {rate:+.1f} cm/hr"
            send_telegram_alert(msg)
            last_low_alert_time = now

def check_three_quarter_alert(percentage, level, estimated):
    """3/4 full sits inside the sensor's blind spot and can never be read
    directly, so this fires off the estimated level instead. Latches so it
    only sends once per fill cycle (see THREE_QUARTER_RESET_PCT)."""
    global three_quarter_alert_sent
    if percentage >= THREE_QUARTER_ALERT_PCT:
        if not three_quarter_alert_sent:
            tag = " (estimated -- sensor can't read this close)" if estimated else ""
            send_telegram_alert(
                f"🪣 *3/4 full*{tag}\nLevel: {percentage:.1f}% ({level:.1f} cm)"
            )
            three_quarter_alert_sent = True
    elif percentage < THREE_QUARTER_RESET_PCT:
        three_quarter_alert_sent = False

def take_trend_sample():
    """Runs every TREND_INTERVAL_SEC: takes one filtered reading, adds it to the
    long-term trend history, recomputes fill rate from that history (not from
    however often the dashboard happens to be loaded), evaluates the high/low
    and 3/4-full thresholds, and pushes a periodic Telegram status update.

    Near full, the water gets closer to the sensor than its ~20cm minimum
    range and get_filtered_reading() starts returning None -- at that point
    the level is estimated by extrapolating the last real reading forward
    using the current fill rate, rather than just reporting a failure."""
    global last_trend_time, fill_rate_cm_per_hr, last_valid_level, last_valid_time
    now = time.time()
    last_trend_time = now

    reading = get_filtered_reading()
    estimated = False

    if reading is not None:
        _, water_level, percentage = reading
        history.append((now, water_level))
        if len(history) > HISTORY_MAX:
            history.pop(0)

        if len(history) >= 2:
            t0, l0 = history[0]
            time_diff_hours = (now - t0) / 3600.0
            if time_diff_hours > 0:
                fill_rate_cm_per_hr = (water_level - l0) / time_diff_hours

        last_valid_level = water_level
        last_valid_time = now
    elif last_valid_level is not None and last_valid_time is not None:
        estimated = True
        hours_since = (now - last_valid_time) / 3600.0
        water_level = last_valid_level + fill_rate_cm_per_hr * hours_since
        water_level = max(0.0, min(TANK_DEPTH_CM, water_level))
        percentage = (water_level / TANK_DEPTH_CM) * 100.0
    else:
        send_telegram_alert("⚠️ Water sensor: reading failed / out of range, no prior data to estimate from.")
        return

    check_thresholds_and_alert(percentage, water_level, fill_rate_cm_per_hr)
    check_three_quarter_alert(percentage, water_level, estimated)

    tag = " (estimated, out of sensor range)" if estimated else ""
    send_telegram_alert(
        f"📊 *Water level update*{tag}\nLevel: {percentage:.1f}% ({water_level:.1f} cm)\nTrend: {fill_rate_cm_per_hr:+.2f} cm/hr"
    )

def get_dashboard_reading():
    """Cheap instant read for the web dashboard. Shows current level/distance
    live on every page load, but reuses the trend-derived fill_rate_cm_per_hr
    rather than recomputing it from consecutive page loads."""
    reading = get_filtered_reading()
    if reading is None:
        return None

    raw_dist, water_level, percentage = reading
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

    sync_time()  # needed before quiet-hours checks mean anything

    # Notify system startup via Telegram
    send_telegram_alert(f"🚀 *Pico W Water Monitor Online*\nIP: `{ip}`")

    addr = socket.getaddrinfo('0.0.0.0', 80)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(2)
    s.settimeout(1.0)  # wake up regularly so trend sampling doesn't depend on a visitor
    print(f"Dashboard listening on http://{ip}:80")

    take_trend_sample()  # seed the trend history and send the first status update

    while True:
        try:
            gc.collect()

            if time.time() - last_trend_time >= TREND_INTERVAL_SEC:
                take_trend_sample()

            try:
                cl, addr = s.accept()
            except OSError:
                continue  # accept() timed out; loop back to the trend check above

            req = cl.recv(1024).decode('utf-8')

            data = get_dashboard_reading()

            if data is None:
                html = "<html><body><h2>Sensor reading error or out of range</h2></body></html>"
            else:
                pct = data['percentage']
                color = "#27ae60" if pct > 30 else ("#f39c12" if pct > 15 else "#e74c3c")
                
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