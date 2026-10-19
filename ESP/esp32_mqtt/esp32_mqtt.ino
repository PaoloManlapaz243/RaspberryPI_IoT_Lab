/*
  ESP32 -> MQTT sensor publisher for the IoT Lab pipeline.

  Connects to the Pi's WiFi hotspot (PiNet) and publishes a JSON reading to the
  Mosquitto broker on the Pi. A bridge process on the Pi subscribes to the topic
  and writes each message into the SQLite `readings` table, after which the
  existing store-and-forward uploader sends it to AWS (ADR 0008).

  Libraries (Arduino Library Manager): PubSubClient, ArduinoJson.
  Board: Tools -> Board -> esp32 -> "ESP32 Dev Module".

  NOTE: this version publishes a TEST payload (a counter + fake temperature) so
  you can verify the whole pipeline before wiring a real sensor. Swap the two
  marked lines for real sensor reads once the chain works end to end.
*/

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

// ---------------- config ----------------
const char* WIFI_SSID = "PiNet";
const char* WIFI_PASS = "raspberry123";        // the hotspot password you set

const char* MQTT_HOST = "10.42.0.1";           // the Pi's hotspot IP (verify: hostname -I)
const int   MQTT_PORT = 1883;
const char* MQTT_TOPIC = "sensors/esp32";
const char* MQTT_CLIENT_ID = "esp32-E1";       // unique per board
const char* SENSOR_ID = "E1";                  // this sensor's id (DynamoDB partition)

const unsigned long PUBLISH_EVERY_MS = 3000;   // 3 s between publishes

WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);
unsigned long lastPublish = 0;
long counter = 0;

void connectWiFi() {
  Serial.printf("WiFi: connecting to %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.printf("\nWiFi: connected, IP %s\n", WiFi.localIP().toString().c_str());
}

void connectMQTT() {
  while (!mqtt.connected()) {
    Serial.printf("MQTT: connecting to %s:%d ...", MQTT_HOST, MQTT_PORT);
    if (mqtt.connect(MQTT_CLIENT_ID)) {
      Serial.println(" connected");
    } else {
      Serial.printf(" failed (rc=%d), retrying in 2s\n", mqtt.state());
      delay(2000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);
  connectWiFi();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) connectWiFi();
  if (!mqtt.connected()) connectMQTT();
  mqtt.loop();

  unsigned long now = millis();
  if (now - lastPublish >= PUBLISH_EVERY_MS) {
    lastPublish = now;
    counter++;

    // Build the JSON payload. The Pi bridge adds the timestamp on receipt
    // (the ESP32 has no real clock on an isolated hotspot), so we only send
    // sensor_id + the measured values.
    StaticJsonDocument<192> doc;
    doc["sensor_id"] = SENSOR_ID;
    doc["count"] = counter;                         // <-- TEST field
    doc["temperature_c"] = 22.0 + (counter % 5);    // <-- TEST fake reading; replace with real sensor

    char buf[192];
    size_t n = serializeJson(doc, buf);

    if (mqtt.publish(MQTT_TOPIC, buf, n)) {
      Serial.printf("published -> %s : %s\n", MQTT_TOPIC, buf);
    } else {
      Serial.println("publish failed");
    }
  }
}
