"""
Carbon Footprint Analyzer for Personal Devices
----------------------------------------------
This application tracks power usage of a laptop/desktop in real-time
and converts those readings to estimated CO2 emissions. A lightweight
Flask dashboard visualizes CPU load, brightness impact, battery drain,
and cumulative carbon output.

All underlying logic remains identical to the baseline system.
"""

import threading
import time
import sqlite3
from datetime import datetime, timedelta

import psutil
from flask import Flask, render_template, jsonify, request

# Optional brightness control
try:
    import screen_brightness_control as sbc
    BRIGHTNESS_AVAILABLE = True
except Exception:
    BRIGHTNESS_AVAILABLE = False

# Databases
DB_LIVE = "carbon_live.db"
DB_SIM = "carbon_sim_5days.db"

# Sampling and energy constants
SAMPLE_INTERVAL_SECONDS = 2
BATTERY_WH_CAPACITY = 50.0
DEFAULT_BRIGHTNESS = 60
CPU_POWER_COEFF = 0.25
BRIGHTNESS_POWER_COEFF = 0.08
BASE_POWER_W = 8.0

# Environmental metrics
COST_PER_KWH_BDT = 8.0
CO2_PER_KWH_KG = 0.7  # kg CO2 per kWh

app = Flask(__name__)

# ---------------------------
# Database Initialization
# ---------------------------

def init_live_db():
    conn = sqlite3.connect(DB_LIVE)
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            cpu_util REAL,
            brightness REAL,
            battery_percent REAL,
            is_plugged INTEGER,
            power_W REAL,
            energy_Wh_cum REAL
        )"""
    )
    conn.commit()
    conn.close()

# ---------------------------
# Sensor & Power Estimation
# ---------------------------

def get_brightness():
    if BRIGHTNESS_AVAILABLE:
        try:
            vals = sbc.get_brightness()
            return float(vals[0] if isinstance(vals, list) else vals)
        except Exception:
            return DEFAULT_BRIGHTNESS
    return DEFAULT_BRIGHTNESS

def estimate_power(cpu_util, brightness, batt, last_batt_percent, last_ts):
    now = time.time()

    # Base power + CPU + brightness impact
    power_linear = BASE_POWER_W + CPU_POWER_COEFF * cpu_util + BRIGHTNESS_POWER_COEFF * (brightness or 0)

    # Battery-based estimation (more accurate when discharging)
    battery_based_power = None
    if batt and not batt.power_plugged:
        if last_batt_percent is not None and last_ts is not None and batt.percent < last_batt_percent:
            dt_h = (now - last_ts) / 3600.0
            if dt_h > 0:
                d_percent = last_batt_percent - batt.percent
                energy_used_Wh = BATTERY_WH_CAPACITY * (d_percent / 100.0)
                battery_based_power = energy_used_Wh / dt_h

    return battery_based_power if battery_based_power is not None else power_linear

# ---------------------------
# Data Collector Thread
# ---------------------------

def collector_thread():
    """Collect real energy data for carbon tracking."""
    conn = sqlite3.connect(DB_LIVE, check_same_thread=False)
    cur = conn.cursor()

    last_ts = None
    last_batt_percent = None
    energy_cum_Wh = 0.0

    while True:
        start = time.time()
        now_dt = datetime.now().isoformat(timespec="seconds")

        cpu_util = psutil.cpu_percent(interval=0.5)
        batt = psutil.sensors_battery()
        batt_percent = float(batt.percent) if batt else None
        is_plugged = 1 if (batt and batt.power_plugged) else 0
        brightness = get_brightness()

        power_W = estimate_power(cpu_util, brightness, batt, last_batt_percent, last_ts)

        now_ts = time.time()
        if last_ts and power_W:
            dt_h = (now_ts - last_ts) / 3600.0
            energy_cum_Wh += power_W * dt_h

        cur.execute(
            """INSERT INTO samples
               (timestamp,cpu_util,brightness,battery_percent,is_plugged,power_W,energy_Wh_cum)
               VALUES (?,?,?,?,?,?,?)""",
            (now_dt, cpu_util, brightness, batt_percent, is_plugged, power_W, energy_cum_Wh),
        )
        conn.commit()

        last_ts, last_batt_percent = now_ts, batt_percent
        time.sleep(max(0, SAMPLE_INTERVAL_SECONDS - (time.time() - start)))

# ---------------------------
# Fetch Samples
# ---------------------------

def fetch_samples(mode="sim", minutes=60):
    if mode == "live":
        conn = sqlite3.connect(DB_LIVE)
        cur = conn.cursor()
        since = datetime.now() - timedelta(minutes=minutes)
        cur.execute(
            """SELECT timestamp,cpu_util,brightness,battery_percent,is_plugged,power_W,energy_Wh_cum
                   FROM samples
                   WHERE timestamp >= ?
                   ORDER BY timestamp""",
            (since.isoformat(timespec="seconds"),)
        )
    else:
        conn = sqlite3.connect(DB_SIM)
        cur = conn.cursor()
        cur.execute(
            """SELECT timestamp,cpu_util,brightness,battery_percent,is_plugged,power_W,energy_Wh_cum
               FROM samples
               ORDER BY id"""
        )

    rows = cur.fetchall()
    conn.close()
    return rows

# ---------------------------
# Flask Routes
# ---------------------------

@app.route("/")
def dashboard():
    return render_template("dashboard.html")  # unchanged template

@app.route("/api/data")
def api_data():
    mode = request.args.get("mode", "sim")
    minutes = request.args.get("minutes", 60, type=int)

    rows = fetch_samples(mode, minutes)
    data = []
    for idx, r in enumerate(rows):
        ts, cpu, bright, batt, plug, power, energy = r
        data.append({
            "index": idx + 1,
            "cpu_util": cpu,
            "brightness": bright,
            "battery_percent": batt,
            "is_plugged": plug,
            "power_W": power,
            "energy_Wh_cum": energy,
        })

    return jsonify(data)

@app.route("/api/summary")
def api_summary():
    mode = request.args.get("mode", "sim")
    minutes = request.args.get("minutes", 60, type=int)

    rows = fetch_samples(mode, minutes)
    if not rows:
        return jsonify({
            "energy_Wh": 0,
            "avg_power_W": 0,
            "avg_cpu": 0,
            "co2_kg": 0
        })

    powers = [r[5] or 0 for r in rows]
    cpus = [r[1] or 0 for r in rows]
    energies = [r[6] or 0 for r in rows]

    energy_Wh = energies[-1] - energies[0] if len(energies) > 1 else energies[-1]
    avg_power = sum(powers) / len(powers)
    avg_cpu = sum(cpus) / len(cpus)

    energy_kWh = energy_Wh / 1000.0
    co2 = energy_kWh * CO2_PER_KWH_KG

    return jsonify({
        "energy_Wh": round(energy_Wh, 2),
        "avg_power_W": round(avg_power, 2),
        "avg_cpu": round(avg_cpu, 1),
        "co2_kg": round(co2, 3)
    })

# ---------------------------
# Main Entry
# ---------------------------

if __name__ == "__main__":
    init_live_db()
    t = threading.Thread(target=collector_thread, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=5000, debug=False)
