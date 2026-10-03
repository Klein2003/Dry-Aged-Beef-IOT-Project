/*
 * Dry Aged Beef Cabinet - ESP32 (Wokwi)
 * ตู้ 3 ชั้น x 3 ช่อง แต่ละช่องมี DHT22 (อุณหภูมิ/ความชื้น) + sensor ตรวจจับเนื้อ
 *
 * ส่งค่าทุกช่องขึ้น MQTT:
 *   topic   : <TOPIC_BASE>/slot/<ชั้น>-<ช่อง>        เช่น dryaged/cabinet1/slot/1-2
 *   payload : {"shelf":1,"slot":2,"meat":true,"temp":2.10,"hum":80.30}
 *   สถานะ   : <TOPIC_BASE>/status = "online" / "offline" (LWT, retained)
 *
 * ฝั่ง Python (app.py / mqtt_bridge.py) จะรับข้อมูลแล้วบันทึกลง SQLite
 */
#include <WiFi.h>
#include <PubSubClient.h>
#include "DHTesp.h"

// ───── WiFi / MQTT ─────
const char *WIFI_SSID = "Wokwi-GUEST";
const char *WIFI_PASS = "";
const char *MQTT_HOST = "broker.hivemq.com";
const uint16_t MQTT_PORT = 1883;
const char *MQTT_USER = "";
const char *MQTT_PASS = "";
const char *TOPIC_BASE = "dryaged/cabinet1";   // ต้องตรงกับ MQTT_TOPIC_BASE ฝั่ง Python
const unsigned long PUBLISH_MS = 5000;         // ส่งค่าทุก 5 วินาที

// ───── Sensors (index 0..8 = ช่อง 1-1, 1-2, 1-3, 2-1, ... 3-3) ─────
const int SHELVES = 3, SLOTS = 3, N = SHELVES * SLOTS;
const int DHT_PINS[N]  = {4, 5, 13, 14, 15, 16, 17, 18, 19};
// sensor ตรวจจับเนื้อ (ใน Wokwi ใช้ slide switch แทน: HIGH = มีเนื้อ)
const int MEAT_PINS[N] = {32, 33, 25, 26, 27, 34, 35, 36, 39};

DHTesp dht[N];
WiFiClient net;
PubSubClient mqtt(net);
char statusTopic[64];
unsigned long lastPublish = 0;

bool isMeatPresent(int i) {
  // ถ้าใช้ sensor ตัวอื่น (IR / Ultrasonic / Load cell) ให้แก้ฟังก์ชันนี้
  return digitalRead(MEAT_PINS[i]) == HIGH;
}

void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.print("Connecting WiFi");
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS, 6);
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    Serial.print(".");
  }
  Serial.printf("\nWiFi OK, IP: %s\n", WiFi.localIP().toString().c_str());
}

void connectMqtt() {
  while (!mqtt.connected()) {
    connectWiFi();
    String clientId = "esp32-dryaged-" + String((uint32_t)ESP.getEfuseMac(), HEX);
    Serial.printf("Connecting MQTT %s:%u ... ", MQTT_HOST, MQTT_PORT);
    bool ok = strlen(MQTT_USER)
      ? mqtt.connect(clientId.c_str(), MQTT_USER, MQTT_PASS, statusTopic, 1, true, "offline")
      : mqtt.connect(clientId.c_str(), statusTopic, 1, true, "offline");
    if (ok) {
      Serial.println("OK");
      mqtt.publish(statusTopic, "online", true);
    } else {
      Serial.printf("failed rc=%d, retry in 2s\n", mqtt.state());
      delay(2000);
    }
  }
}

void publishSlots() {
  char topic[64], payload[128];
  for (int i = 0; i < N; i++) {
    int shelf = i / SLOTS + 1, slot = i % SLOTS + 1;
    TempAndHumidity v = dht[i].getTempAndHumidity();
    if (dht[i].getStatus() != DHTesp::ERROR_NONE || isnan(v.temperature) || isnan(v.humidity)) {
      Serial.printf("Slot %d-%d: DHT error %s\n", shelf, slot, dht[i].getStatusString());
      continue;
    }
    bool meat = isMeatPresent(i);
    snprintf(topic, sizeof(topic), "%s/slot/%d-%d", TOPIC_BASE, shelf, slot);
    snprintf(payload, sizeof(payload),
             "{\"shelf\":%d,\"slot\":%d,\"meat\":%s,\"temp\":%.2f,\"hum\":%.2f}",
             shelf, slot, meat ? "true" : "false", v.temperature, v.humidity);
    mqtt.publish(topic, payload);
    Serial.printf("%s %s\n", topic, payload);
  }
}

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < N; i++) {
    dht[i].setup(DHT_PINS[i], DHTesp::DHT22);
    pinMode(MEAT_PINS[i], INPUT);
  }
  snprintf(statusTopic, sizeof(statusTopic), "%s/status", TOPIC_BASE);
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(512);
  connectMqtt();
}

void loop() {
  if (!mqtt.connected()) connectMqtt();
  mqtt.loop();
  if (millis() - lastPublish >= PUBLISH_MS) {
    lastPublish = millis();
    publishSlots();
  }
}
