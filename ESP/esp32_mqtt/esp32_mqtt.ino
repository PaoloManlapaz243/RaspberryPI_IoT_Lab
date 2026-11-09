/*
  ESP32 <-> MQTT for the IoT Lab pipeline: publishes sensor readings UP, and
  receives commands DOWN to drive an actuator (docs/adr/0012-command-and-control.md).

  UP   (Ben's original, feat/esp_mqtt): connects to the Pi's WiFi hotspot (PiNet)
       and publishes a JSON reading to `sensors/esp32` on the Pi's Mosquitto
       broker. The Pi-side bridge writes each message into the SQLite `readings`
       table, and the store-and-forward uploader sends it to AWS.
  DOWN (added): subscribes to `commands/esp32`. The Pi's agent publishes
       {"command_id", "actuator": "cooling", "state": "on"|"off", ...} there,
       only after its safety guard passed. This sketch checks the command AGAIN
       against its own allow-list (defense in depth: anyone on the hotspot can
       publish to the open broker), drives the actuator pin, and acknowledges on
       `sensors/esp32/ack` with {"command_id", "ok", ...} so the agent can say
       "confirmed". The ack topic is two levels deep, so the bridge's `sensors/+`
       subscription does NOT ingest it as a reading.

  Safety on the device:
    - boots with the actuator OFF
    - only "cooling" with state "on"/"off" is accepted; anything else is refused
      (and the refusal is acked, so the agent reports it)
    - failsafe: cooling switches itself off after MAX_ON_MS, even if the "off"
      command never arrives (Pi crashed, WiFi dropped)

  Libraries (Arduino Library Manager): PubSubClient, ArduinoJson (v6 API).
  Board: Tools -> Board -> esp32 -> "ESP32 Dev Module".

  Wiring for the demo: none -- COOLING_PIN 2 is the onboard LED on most ESP32
  dev boards. For a relay module: IN -> a free GPIO (set COOLING_PIN), VCC -> 5V
  (or 3.3V, per the module), GND -> GND; many relay modules switch on a LOW
  input, so set RELAY_ACTIVE_LOW accordingly.

  NOTE: the reading is still a TEST payload (a counter + fake temperature) so
  the whole pipeline can be verified before wiring a real sensor. Swap the two
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
const char* MQTT_TOPIC = "sensors/esp32";      // readings UP
const char* COMMAND_TOPIC = "commands/esp32";  // commands DOWN
const char* ACK_TOPIC = "sensors/esp32/ack";   // command acknowledgments UP
const char* MQTT_CLIENT_ID = "esp32-E1";       // unique per board
const char* SENSOR_ID = "E1";                  // this sensor's id (DynamoDB partition)

const unsigned long PUBLISH_EVERY_MS = 3000;   // 3 s between publishes

// Actuator
const int  COOLING_PIN = 2;                    // onboard LED for the demo; relay IN pin for real
const bool RELAY_ACTIVE_LOW = false;           // true for relay modules that switch on LOW
const unsigned long MAX_ON_MS = 10UL * 60UL * 1000UL;   // failsafe: auto-off after 10 min

WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);
unsigned long lastPublish = 0;
long counter = 0;

bool coolingOn = false;
unsigned long coolingOnSince = 0;

void setCooling(bool on) {
  coolingOn = on;
  if (on) coolingOnSince = millis();
  // XOR: drive HIGH for "on" unless the relay switches on LOW
  digitalWrite(COOLING_PIN, (on != RELAY_ACTIVE_LOW) ? HIGH : LOW);
  Serial.printf("cooling -> %s\n", on ? "ON" : "OFF");
}

void publishAck(const char* commandId, bool ok, const char* state, const char* error) {
  StaticJsonDocument<192> ack;
  ack["sensor_id"] = SENSOR_ID;
  ack["command_id"] = commandId;
  ack["actuator"] = "cooling";
  ack["ok"] = ok;
  ack["cooling_on"] = coolingOn;               // the actual state after handling
  if (state) ack["state"] = state;
  if (error) ack["error"] = error;

  char buf[192];
  size_t n = serializeJson(ack, buf);
  mqtt.publish(ACK_TOPIC, buf, n);
  Serial.printf("ack -> %s : %s\n", ACK_TOPIC, buf);
}

// Called by PubSubClient (from mqtt.loop()) for every message on a subscribed topic
void onMessage(char* topic, byte* payload, unsigned int length) {
  if (strcmp(topic, COMMAND_TOPIC) != 0) return;

  // const input: ArduinoJson COPIES the strings. With a writable byte* it would
  // point into PubSubClient's buffer ("zero-copy"), which publishAck() reuses,
  // corrupting command_id before the ack is built.
  StaticJsonDocument<256> cmd;
  DeserializationError err = deserializeJson(cmd, (const byte*)payload, length);
  if (err) {
    Serial.printf("command: bad JSON (%s), ignored\n", err.c_str());
    return;                                    // no command_id to ack against
  }

  const char* commandId = cmd["command_id"] | "";
  const char* actuator  = cmd["actuator"]   | "";
  const char* state     = cmd["state"]      | "";
  Serial.printf("command %s: %s -> %s\n", commandId, actuator, state);

  // Device-side allow-list: the same rule as ACTUATORS in commands.py
  if (strcmp(actuator, "cooling") != 0) {
    publishAck(commandId, false, state, "unknown actuator");
    return;
  }
  if (strcmp(state, "on") == 0) {
    setCooling(true);
  } else if (strcmp(state, "off") == 0) {
    setCooling(false);
  } else {
    publishAck(commandId, false, state, "state must be on or off");
    return;
  }
  publishAck(commandId, true, state, nullptr);
}

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
      // (Re)subscribe on every connect: a reconnect starts with no subscriptions
      mqtt.subscribe(COMMAND_TOPIC, 1);
      Serial.printf("MQTT: subscribed to %s\n", COMMAND_TOPIC);
    } else {
      Serial.printf(" failed (rc=%d), retrying in 2s\n", mqtt.state());
      delay(2000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);
  pinMode(COOLING_PIN, OUTPUT);
  setCooling(false);                           // boot safe: actuator off
  connectWiFi();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) connectWiFi();
  if (!mqtt.connected()) connectMQTT();
  mqtt.loop();

  unsigned long now = millis();

  // Failsafe: never leave the actuator on indefinitely
  if (coolingOn && now - coolingOnSince >= MAX_ON_MS) {
    Serial.println("failsafe: cooling on too long, switching off");
    setCooling(false);
  }

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
    doc["cooling_on"] = coolingOn;                  // actuator state, logged with each reading

    char buf[192];
    size_t n = serializeJson(doc, buf);

    if (mqtt.publish(MQTT_TOPIC, buf, n)) {
      Serial.printf("published -> %s : %s\n", MQTT_TOPIC, buf);
    } else {
      Serial.println("publish failed");
    }
  }
}
