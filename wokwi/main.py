import machine
import dht
import time
import json
import network
import ubinascii
from machine import Pin, SoftI2C
from neopixel import NeoPixel
from umqtt.simple import MQTTClient
import ssd1306  # ต้องเพิ่มไฟล์ ssd1306.py ในโปรเจกต์ Wokwi ด้วย

# ───── WiFi / MQTT ─────
WIFI_SSID, WIFI_PASS = "Wokwi-GUEST", ""
MQTT_HOST, MQTT_PORT = "broker.hivemq.com", 1883
MQTT_USER, MQTT_PASS = "", ""
TOPIC_BASE = "dryaged/cabinet1"     # ต้องตรงกับ MQTT_TOPIC_BASE ฝั่ง Python
TOPIC_STATUS = "{}/status".format(TOPIC_BASE)
PUBLISH_MS = 5000                   # ส่งค่าทุกช่องขึ้น MQTT ทุก 5 วินาที
RECONNECT_MS = 5000                 # ถ้าหลุด ลองเชื่อมต่อใหม่ทุก 5 วินาที

# ───── ตู้ 3 ชั้น x 3 ช่อง (index 0..8 = ช่อง 1-1, 1-2, 1-3, 2-1, ... 3-3) ─────
SHELVES, SLOTS = 3, 3
DHT_PINS = [14, 4, 5, 16, 17, 18, 19, 25, 26]
ECHO_PINS = [32, 34, 35, 36, 39, 27, 21, 22, 23]
TRIG_PIN = 33                       # TRIG ของ HC-SR04 ทุกตัวต่อร่วมขาเดียวกัน
NEOPIXEL_PIN = 15                   # ไฟสถานะ 3x3 (1 ดวงต่อ 1 ช่อง)

# เกณฑ์ตู้แช่เนื้อ (อุณหภูมิ 1-3°C, ความชื้น 75-85%)
T_MIN, T_MAX = 1.0, 3.0
H_MIN, H_MAX = 75.0, 85.0
DETECTION_DIST = 100.0      # มีเนื้อเมื่อระยะ 0-100 ซม.
ECHO_TIMEOUT_US = 30000     # ~5 เมตร ไกลกว่านี้ถือว่าไม่มีเนื้อ

LOOP_DELAY = 0.5            # รอบลูป 0.5 วินาที (ให้ไฟเหลืองกระพริบ)
DHT_INTERVAL_MS = 2000      # DHT22 อ่านได้ไม่ถี่กว่า 2 วินาทีต่อครั้ง
DETAIL_MS = 2000            # OLED สลับแสดงรายละเอียดทีละช่องทุก 2 วินาที

# สีไฟสถานะ (R, G, B)
GREEN, RED, YELLOW, OFF = (0, 60, 0), (60, 0, 0), (60, 40, 0), (0, 0, 0)

# 1. หน้าจอ OLED (พิน 12=SDA, 13=SCL)
i2c = SoftI2C(scl=Pin(13), sda=Pin(12))
display = ssd1306.SSD1306_I2C(128, 64, i2c)

# 2. DHT22 + HC-SR04 ของแต่ละช่อง
trigger = Pin(TRIG_PIN, Pin.OUT)
trigger.value(0)
slots = []
for i in range(SHELVES * SLOTS):
    slots.append({
        "shelf": i // SLOTS + 1, "slot": i % SLOTS + 1,
        "dht": dht.DHT22(Pin(DHT_PINS[i])),
        "echo": Pin(ECHO_PINS[i], Pin.IN),
        "t": None, "h": None, "meat": False, "alerts": [],
    })

# 3. ไฟสถานะ NeoPixel 3x3
pixels = NeoPixel(Pin(NEOPIXEL_PIN), SHELVES * SLOTS)

blink = 0
mqtt = None
mqtt_ok = False


def get_distance(echo):
    """วัดระยะทางจาก HC-SR04 ของช่องนั้น หน่วยเซนติเมตร"""
    trigger.value(0)
    time.sleep_us(5)
    trigger.value(1)
    time.sleep_us(10)
    trigger.value(0)

    duration = machine.time_pulse_us(echo, 1, ECHO_TIMEOUT_US)
    if duration < 0:
        return 999.0  # สัญญาณหลุด ถือว่าไม่มีเนื้อ
    return (duration * 0.0343) / 2


def read_all_dht():
    for s in slots:
        try:
            s["dht"].measure()
            s["t"], s["h"] = s["dht"].temperature(), s["dht"].humidity()
        except OSError as e:
            print("DHT22 ช่อง {}-{} อ่านค่าไม่ได้ ใช้ค่าเดิม: {}".format(s["shelf"], s["slot"], e))


def check_alerts(t, h):
    alerts = []
    if t < T_MIN:
        alerts.append("T.LOW {:.1f}C".format(t - T_MIN))
    elif t > T_MAX:
        alerts.append("T.HIGH +{:.1f}C".format(t - T_MAX))
    if h < H_MIN:
        alerts.append("H.LOW {:.0f}%".format(h - H_MIN))
    elif h > H_MAX:
        alerts.append("H.HIGH +{:.0f}%".format(h - H_MAX))
    return alerts


def status_code(s):
    """รหัสสั้น 2 ตัวอักษรสำหรับแสดงบน OLED"""
    if s["t"] is None:
        return "??"
    if not s["meat"]:
        return "--"
    if not s["alerts"]:
        return "OK"
    has_t = any(a.startswith("T") for a in s["alerts"])
    has_h = any(a.startswith("H") for a in s["alerts"])
    return "!!" if has_t and has_h else "!T" if has_t else "!H"


def show_message(line1, line2=""):
    display.fill(0)
    display.text(line1, 0, 0, 1)
    display.text(line2, 0, 16, 1)
    display.show()


def update_display(detail):
    meat_count = sum(1 for s in slots if s["meat"])
    display.fill(0)
    display.text("Meat {}/9 MQTT:{}".format(meat_count, "OK" if mqtt_ok else "--"), 0, 0, 1)
    for shelf in range(SHELVES):
        row = slots[shelf * SLOTS:(shelf + 1) * SLOTS]
        display.text("L{}: ".format(shelf + 1) + "  ".join(status_code(s) for s in row),
                     0, 12 + shelf * 10, 1)
    if detail is not None:
        s = detail
        display.text("{}-{} T{:.1f} H{:.0f}".format(s["shelf"], s["slot"], s["t"], s["h"]), 0, 46, 1)
        display.text(s["alerts"][0] if s["alerts"] else "Keeping", 0, 56, 1)
    else:
        display.text("No meat", 0, 46, 1)
    display.show()


def update_leds():
    for i, s in enumerate(slots):
        if not s["meat"]:
            pixels[i] = RED                           # ไม่มีเนื้อ: แดง
        elif s["alerts"]:
            pixels[i] = YELLOW if blink else OFF      # ผิดปกติ: เหลืองกระพริบ
        else:
            pixels[i] = GREEN                         # ปกติ: เขียว
    pixels.write()


def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("กำลังเชื่อมต่อ WiFi", end="")
        wlan.connect(WIFI_SSID, WIFI_PASS)
        while not wlan.isconnected():
            print(".", end="")
            time.sleep(0.25)
    print("\nWiFi OK:", wlan.ifconfig()[0])


def connect_mqtt():
    """เชื่อมต่อ MQTT broker คืนค่า True ถ้าสำเร็จ"""
    global mqtt, mqtt_ok
    try:
        client_id = b"dryaged-" + ubinascii.hexlify(machine.unique_id())
        mqtt = MQTTClient(client_id, MQTT_HOST, port=MQTT_PORT,
                          user=MQTT_USER or None, password=MQTT_PASS or None,
                          keepalive=60)
        mqtt.set_last_will(TOPIC_STATUS, b"offline", retain=True)
        mqtt.connect()
        mqtt.publish(TOPIC_STATUS, b"online", retain=True)
        mqtt_ok = True
        print("MQTT OK:", MQTT_HOST, "->", TOPIC_BASE + "/slot/<ชั้น>-<ช่อง>")
    except OSError as e:
        mqtt_ok = False
        print("เชื่อมต่อ MQTT ไม่ได้:", e)
    return mqtt_ok


def publish_all():
    """ส่งค่าของทุกช่องขึ้น MQTT (ฝั่ง Python จะเป็นคนตัดสินใจบันทึกลง SQLite)"""
    global mqtt_ok
    for s in slots:
        if s["t"] is None:
            continue  # ยังไม่เคยอ่าน DHT22 ของช่องนี้ได้
        topic = "{}/slot/{}-{}".format(TOPIC_BASE, s["shelf"], s["slot"])
        payload = json.dumps({"shelf": s["shelf"], "slot": s["slot"], "meat": s["meat"],
                              "temp": round(s["t"], 2), "hum": round(s["h"], 2)})
        try:
            mqtt.publish(topic, payload)
        except OSError as e:
            mqtt_ok = False
            print("ส่ง MQTT ไม่สำเร็จ:", e)
            return
    print("[MQTT] ส่งค่าทั้ง 9 ช่องแล้ว")


print("ระบบตู้แช่เนื้ออัจฉริยะ (9 ช่อง) เริ่มทำงาน...")
pixels.fill(OFF)
pixels.write()
show_message("Connecting WiFi", "Please wait")
connect_wifi()
show_message("Connecting MQTT", MQTT_HOST[:16])
connect_mqtt()
show_message("Starting...", "Please wait")

time.sleep(2)               # รอให้ DHT22 พร้อม
read_all_dht()
last_dht = time.ticks_ms()
last_publish = time.ticks_add(time.ticks_ms(), -PUBLISH_MS)   # ส่งรอบแรกทันที
last_reconnect = time.ticks_ms()
last_detail = time.ticks_ms()
detail_idx = 0

while True:
    try:
        # อ่าน DHT22 ทุกช่องเมื่อครบ 2 วินาที ไม่งั้นใช้ค่าล่าสุด
        if time.ticks_diff(time.ticks_ms(), last_dht) >= DHT_INTERVAL_MS:
            read_all_dht()
            last_dht = time.ticks_ms()

        blink = 1 - blink
        for s in slots:
            dist = get_distance(s["echo"])
            s["meat"] = (0.0 < dist <= DETECTION_DIST)
            prev = s["alerts"]
            s["alerts"] = check_alerts(s["t"], s["h"]) if s["meat"] and s["t"] is not None else []
            if s["alerts"] and s["alerts"] != prev:
                print("[ALERT] ช่อง {}-{} ผิดปกติ -> {} | T={:.1f}C H={:.0f}%".format(
                    s["shelf"], s["slot"], " ".join(s["alerts"]), s["t"], s["h"]))
                print("[SYSTEM] กำลังปรับระบบทำความเย็น/ความชื้นของช่องนี้อัตโนมัติ...")
            elif prev and not s["alerts"] and s["meat"]:
                print("[OK] ช่อง {}-{} กลับเข้าเกณฑ์แล้ว".format(s["shelf"], s["slot"]))

        # OLED: ภาพรวม 3x3 + สลับรายละเอียดทีละช่องที่มีเนื้อ (ช่องที่ผิดปกติแสดงก่อน)
        ready = [s for s in slots if s["meat"] and s["t"] is not None]
        shown = [s for s in ready if s["alerts"]] or ready
        if time.ticks_diff(time.ticks_ms(), last_detail) >= DETAIL_MS:
            detail_idx += 1
            last_detail = time.ticks_ms()
        update_display(shown[detail_idx % len(shown)] if shown else None)
        update_leds()

        # ── MQTT ──
        now = time.ticks_ms()
        if mqtt_ok:
            if time.ticks_diff(now, last_publish) >= PUBLISH_MS:
                last_publish = now
                publish_all()
        elif time.ticks_diff(now, last_reconnect) >= RECONNECT_MS:
            last_reconnect = now
            connect_wifi()
            connect_mqtt()

    except Exception as e:
        print("เกิดข้อผิดพลาดในลูปนี้:", e)

    time.sleep(LOOP_DELAY)
