"""ค่าคงที่ที่ใช้ร่วมกันระหว่าง app.py, storage.py และ mqtt_bridge.py"""
import os

# ───────────── ค่าที่เหมาะสม ─────────────
TEMP_MIN, TEMP_MAX = 1.0, 3.0      # °C
HUM_MIN, HUM_MAX = 75.0, 85.0      # %RH
SHELVES, SLOTS = 3, 3
METRICS = {
    "temp": ("อุณหภูมิ", "°C", TEMP_MIN, TEMP_MAX),
    "hum": ("ความชื้น", "%", HUM_MIN, HUM_MAX),
}

# ───────────── MQTT / SQLite (override ได้ด้วย env หรือ .streamlit/secrets.toml) ─────────────
DEFAULTS = {
    "MQTT_HOST": "broker.hivemq.com",
    "MQTT_PORT": "1883",
    "MQTT_TOPIC_BASE": "dryaged/cabinet1",   # ต้องตรงกับ TOPIC_BASE ใน sketch.ino
    "MQTT_USERNAME": "",
    "MQTT_PASSWORD": "",
    "DB_PATH": "dryaged.db",
    "HOURLY_LOG_SEC": "3600",                 # เก็บข้อมูลปกติทุก 1 ชั่วโมง
    "ALARM_LOG_SEC": "60",                    # ระหว่างผิดปกติ เก็บซ้ำทุก 60 วินาที
}


def env(key):
    return os.getenv(key, DEFAULTS.get(key))


def deviation(value, lo, hi):
    if value > hi:
        return "สูง", value - hi
    if value < lo:
        return "ต่ำ", lo - value
    return None, 0.0


def issues_of(meat, temp, hum):
    """คืน dict ของ metric ที่ผิดปกติ {metric: (ทิศทาง, ส่วนต่าง)} (เฉพาะช่องที่มีเนื้อ)"""
    if not meat:
        return {}
    out = {}
    for m, value in (("temp", temp), ("hum", hum)):
        _, _, lo, hi = METRICS[m]
        direction, diff = deviation(value, lo, hi)
        if direction:
            out[m] = (direction, diff)
    return out


def describe_issues(issues):
    return ", ".join(f"{METRICS[m][0]}{d}กว่าช่วง {v:.1f}{METRICS[m][1]}"
                     for m, (d, v) in issues.items())
