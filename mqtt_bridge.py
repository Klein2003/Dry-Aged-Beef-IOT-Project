"""รับข้อมูลจาก ESP32 ผ่าน MQTT แล้วส่งต่อให้ Recorder บันทึกลง SQLite

Topic   : <MQTT_TOPIC_BASE>/slot/<ชั้น>-<ช่อง>     เช่น dryaged/cabinet1/slot/2-3
Payload : {"shelf": 2, "slot": 3, "meat": true, "temp": 2.15, "hum": 80.4}

รันแบบไม่มีหน้าเว็บ (เก็บข้อมูลอย่างเดียว):  python mqtt_bridge.py
"""
import json
import threading
import time
import uuid
from datetime import datetime

import paho.mqtt.client as mqtt

from config import SHELVES, SLOTS


class MqttBridge:
    def __init__(self, host, port, base, recorder=None, username=None, password=None):
        self.host, self.port, self.base = host, int(port), base.rstrip("/")
        self.recorder = recorder
        self.latest = {}            # (ชั้น, ช่อง) -> {"meat", "temp", "hum", "t"}
        self.device_status = None   # "online" / "offline" จาก LWT ของ ESP32
        self.connected = False
        self.last_error = None
        self.lock = threading.Lock()

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f"dryaged-server-{uuid.uuid4().hex[:8]}")
        if username:
            self.client.username_pw_set(username, password or None)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def start(self):
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()
        return self

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()

    # ── callbacks ──
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.connected, self.last_error = False, str(reason_code)
            return
        self.connected, self.last_error = True, None
        client.subscribe(f"{self.base}/slot/+", qos=1)
        client.subscribe(f"{self.base}/status", qos=1)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False
        if reason_code.is_failure:
            self.last_error = str(reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            if msg.topic == f"{self.base}/status":
                self.device_status = msg.payload.decode(errors="ignore")
                return
            data = json.loads(msg.payload)
            s, c = (int(x) for x in msg.topic.rsplit("/", 1)[1].split("-"))
            if not (1 <= s <= SHELVES and 1 <= c <= SLOTS):
                return
            meat = bool(data["meat"])
            temp, hum = float(data["temp"]), float(data["hum"])
        except (ValueError, KeyError, TypeError) as e:
            self.last_error = f"payload ไม่ถูกต้อง ({msg.topic}): {e}"
            return

        now = datetime.now()
        with self.lock:
            self.latest[(s, c)] = {"meat": meat, "temp": temp, "hum": hum, "t": now}
        if self.recorder is not None:
            try:
                self.recorder.process((s, c), meat, temp, hum, now)
            except Exception as e:  # ไม่ให้ error ของ DB ทำให้ MQTT thread ตาย
                self.last_error = f"บันทึก DB ไม่สำเร็จ: {e}"

    def snapshot(self):
        with self.lock:
            return dict(self.latest)


if __name__ == "__main__":
    from config import env
    from storage import Recorder

    rec = Recorder(env("DB_PATH"), int(env("HOURLY_LOG_SEC")), int(env("ALARM_LOG_SEC")))
    bridge = MqttBridge(env("MQTT_HOST"), env("MQTT_PORT"), env("MQTT_TOPIC_BASE"), rec,
                        env("MQTT_USERNAME"), env("MQTT_PASSWORD")).start()
    print(f"กำลังฟัง {env('MQTT_TOPIC_BASE')}/slot/+ ที่ {env('MQTT_HOST')} → {env('DB_PATH')}")
    try:
        while True:
            time.sleep(10)
            print(f"[{datetime.now():%H:%M:%S}] connected={bridge.connected} "
                  f"device={bridge.device_status} slots={len(bridge.latest)} "
                  f"err={bridge.last_error}")
    except KeyboardInterrupt:
        bridge.stop()
