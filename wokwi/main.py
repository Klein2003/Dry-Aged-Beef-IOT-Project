import machine
import dht
import time
import json
import network
import ubinascii
from machine import Pin, SoftI2C
from umqtt.simple import MQTTClient
import ssd1306  # ต้องเพิ่มไฟล์ ssd1306.py ในโปรเจกต์ Wokwi ด้วย

# ───── ตำแหน่งของบอร์ดนี้ในตู้ (1 บอร์ด = 1 ช่อง) ─────
SHELF, SLOT = 1, 1

# ───── WiFi / MQTT ─────
WIFI_SSID, WIFI_PASS = "Wokwi-GUEST", ""
MQTT_HOST, MQTT_PORT = "broker.hivemq.com", 1883
MQTT_USER, MQTT_PASS = "", ""
TOPIC_BASE = "dryaged/cabinet1"     # ต้องตรงกับ MQTT_TOPIC_BASE ฝั่ง Python
TOPIC_DATA = "{}/slot/{}-{}".format(TOPIC_BASE, SHELF, SLOT)
TOPIC_STATUS = "{}/status".format(TOPIC_BASE)
PUBLISH_MS = 5000                   # ส่งค่าขึ้น MQTT ทุก 5 วินาที
RECONNECT_MS = 5000                 # ถ้าหลุด ลองเชื่อมต่อใหม่ทุก 5 วินาที

# 1. ตั้งค่าหน้าจอ OLED (พิน 12=SDA, 13=SCL ตามวงจร)
i2c = SoftI2C(scl=Pin(13), sda=Pin(12))
display = ssd1306.SSD1306_I2C(128, 64, i2c)

# 2. ตั้งค่าเซนเซอร์ DHT22 (พิน 14)
dht_sensor = dht.DHT22(Pin(14))

# 3. ตั้งค่าเซนเซอร์อัลตร้าโซนิก HC-SR04 (Trig=33, Echo=32 ตามสายใน diagram.json)
trigger = Pin(33, Pin.OUT)
echo = Pin(32, Pin.IN)

# 4. ตั้งค่าพินไฟ LED
led_green = Pin(25, Pin.OUT)
led_red = Pin(26, Pin.OUT)
led_yellow = Pin(27, Pin.OUT)

led_green.value(0)
led_red.value(0)
led_yellow.value(0)

# เกณฑ์ตู้แช่เนื้อ (อุณหภูมิ 1-3°C, ความชื้น 75-85%)
T_MIN, T_MAX = 1.0, 3.0
H_MIN, H_MAX = 75.0, 85.0
DETECTION_DIST = 100.0      # มีเนื้อเมื่อระยะ 0-100 ซม.

LOOP_DELAY = 0.5            # รอบลูป 0.5 วินาที (ให้ไฟเหลืองกระพริบ)
DHT_INTERVAL_MS = 2000      # DHT22 อ่านได้ไม่ถี่กว่า 2 วินาทีต่อครั้ง

yellow_state = 0
mqtt = None
mqtt_ok = False


def get_distance():
    """วัดระยะทางจาก HC-SR04 หน่วยเซนติเมตร"""
    trigger.value(0)
    time.sleep_us(5)
    trigger.value(1)
    time.sleep_us(10)
    trigger.value(0)

    duration = machine.time_pulse_us(echo, 1, 60000)
    if duration < 0:
        return 999.0  # สัญญาณหลุด ถือว่าไม่มีเนื้อ
    return (duration * 0.0343) / 2


def read_dht():
    """อ่านค่า DHT22 คืนค่า (อุณหภูมิ, ความชื้น)"""
    dht_sensor.measure()
    return dht_sensor.temperature(), dht_sensor.humidity()


def show_message(line1, line2=""):
    display.fill(0)
    display.text(line1, 0, 0, 1)
    display.text(line2, 0, 16, 1)
    display.show()


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
        print("MQTT OK:", MQTT_HOST, "->", TOPIC_DATA)
    except OSError as e:
        mqtt_ok = False
        print("เชื่อมต่อ MQTT ไม่ได้:", e)
    return mqtt_ok


def publish(has_meat, t, h):
    """ส่งค่าของช่องนี้ขึ้น MQTT (ฝั่ง Python จะเป็นคนตัดสินใจบันทึกลง SQLite)"""
    global mqtt_ok
    payload = json.dumps({"shelf": SHELF, "slot": SLOT, "meat": has_meat,
                          "temp": round(t, 2), "hum": round(h, 2)})
    try:
        mqtt.publish(TOPIC_DATA, payload)
        print("[MQTT]", TOPIC_DATA, payload)
    except OSError as e:
        mqtt_ok = False
        print("ส่ง MQTT ไม่สำเร็จ:", e)


print("ระบบตู้แช่เนื้ออัจฉริยะ เริ่มทำงาน...")
show_message("Connecting WiFi", "Please wait")
connect_wifi()
show_message("Connecting MQTT", MQTT_HOST[:16])
connect_mqtt()
show_message("Starting...", "Please wait")

# รอให้ DHT22 พร้อม แล้วอ่านค่าครั้งแรก (ลองซ้ำจนกว่าจะอ่านได้)
t, h = None, None
while t is None:
    time.sleep(2)
    try:
        t, h = read_dht()
    except OSError as e:
        print("รออ่าน DHT22 ครั้งแรก:", e)
last_dht = time.ticks_ms()
last_publish = time.ticks_add(time.ticks_ms(), -PUBLISH_MS)   # ส่งรอบแรกทันที
last_reconnect = time.ticks_ms()

while True:
    try:
        # อ่าน DHT22 เฉพาะเมื่อครบ 2 วินาที ไม่งั้นใช้ค่าล่าสุด
        if time.ticks_diff(time.ticks_ms(), last_dht) >= DHT_INTERVAL_MS:
            try:
                t, h = read_dht()
            except OSError as e:
                print("DHT22 อ่านค่าไม่ได้ ใช้ค่าเดิม:", e)
            last_dht = time.ticks_ms()

        dist = get_distance()
        has_meat = (0.0 < dist <= DETECTION_DIST)

        display.fill(0)

        if has_meat:
            # มีเนื้อ: ไฟเขียวติด ไฟแดงดับ
            led_green.value(1)
            led_red.value(0)

            alerts = []
            if t < T_MIN:
                alerts.append("T.LOW {:.1f}C".format(t - T_MIN))
            elif t > T_MAX:
                alerts.append("T.HIGH +{:.1f}C".format(t - T_MAX))

            if h < H_MIN:
                alerts.append("H.LOW {:.0f}%".format(h - H_MIN))
            elif h > H_MAX:
                alerts.append("H.HIGH +{:.0f}%".format(h - H_MAX))

            if alerts:
                # ค่าหลุดเกณฑ์: ไฟเหลืองกระพริบ
                yellow_state = 1 - yellow_state
                led_yellow.value(yellow_state)

                alert_msg = " ".join(alerts)
                print("[ALERT] ผิดปกติ -> {} | T={:.1f}C H={:.0f}%".format(alert_msg, t, h))
                print("[SYSTEM] กำลังปรับระบบทำความเย็น/ความชื้นอัตโนมัติ...")

                display.text("[ALERT]", 0, 0, 1)
                display.text("Adjusting...", 0, 44, 1)
            else:
                yellow_state = 0
                led_yellow.value(0)
                display.text("[MEAT OK]", 0, 0, 1)
                display.text("Keeping", 0, 44, 1)

            display.text("Temp : {:.1f} C".format(t), 0, 16, 1)
            display.text("Humid: {:.0f} %".format(h), 0, 30, 1)

        else:
            # ไม่มีเนื้อ: ไฟเขียวดับ ไฟแดงติด ไฟเหลืองดับ
            led_green.value(0)
            led_red.value(1)
            yellow_state = 0
            led_yellow.value(0)

            display.text("[EMPTY]", 0, 0, 1)
            display.text("Temp : {:.1f} C".format(t), 0, 16, 1)
            display.text("Humid: {:.0f} %".format(h), 0, 30, 1)

        # บรรทัดล่างสุด: ตำแหน่งช่อง + สถานะ MQTT
        display.text("S{}-{} MQTT:{}".format(SHELF, SLOT, "OK" if mqtt_ok else "--"), 0, 56, 1)
        display.show()

        # ── MQTT ──
        now = time.ticks_ms()
        if mqtt_ok:
            if time.ticks_diff(now, last_publish) >= PUBLISH_MS:
                last_publish = now
                publish(has_meat, t, h)
        elif time.ticks_diff(now, last_reconnect) >= RECONNECT_MS:
            last_reconnect = now
            connect_wifi()
            connect_mqtt()

    except Exception as e:
        print("เกิดข้อผิดพลาดในลูปนี้:", e)

    time.sleep(LOOP_DELAY)
