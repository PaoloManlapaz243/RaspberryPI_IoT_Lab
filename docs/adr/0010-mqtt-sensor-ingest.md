# ADR 0010: Networked sensor ingest over MQTT (local broker)

- **Status:** Accepted
- **Date:** 2026-10-19
- **Extends:** [ADR 0009](0009-sensor-readings.md) (generic `readings` table and
  multi-stream forwarder). No change to detections or to the cloud path.

## Context

Sensors are moving off the Pi onto separate networked boards (first: an ESP32
publishing over WiFi; more to follow). Unlike the DHT11, which is wired to the
Pi's GPIO and read by the `DHTSensor` thread, these boards have no physical
connection to the Pi -- they reach it over the network.

## Options considered

| Option | Summary | Verdict |
|---|---|---|
| **A. ESP32 publishes straight to AWS IoT Core** | Each board its own AWS thing + certs, via an IoT rule to DynamoDB | Rejected. Bypasses the SQLite source-of-truth and store-and-forward (ADR 0002); an offline board must buffer in its own flash, and we lose the durability guarantee. A cert per board, too. |
| **B. ESP32 -> local broker on the Pi -> bridge -> SQLite -> existing forwarder** | The Pi runs Mosquitto; a bridge writes received messages into `readings` | **Chosen.** Reuses the whole pipeline; the board needs no AWS credentials and inherits SQLite durability + at-least-once cloud delivery. |

Within B, the bridge is a **thread in `main.py`**, not a separate process, to
match how `DHTSensor` was integrated (ADR 0009, 2026-10-19 revision): one
program, one shutdown, and it can wake the forwarder directly via `inserted`.

## Decision

1. **Mosquitto on the Pi** is the local broker (port 1883). On the isolated Pi
   WiFi hotspot it runs open (`allow_anonymous`, listener on `0.0.0.0`); that is
   acceptable only because the hotspot is a closed, single-purpose network.
2. **Boards join the Pi's WiFi hotspot** and publish JSON to `sensors/<name>`
   (e.g. `sensors/esp32`), sending `sensor_id` + measured values, no timestamp.
3. **`MQTTBridge`** (a thread, `iot_lab/threads/mqtt_bridge.py`) subscribes to
   `sensors/+` and writes a `readings` row per message: it **stamps the time on
   receipt** (networked sensors have no reliable clock), pulls
   `sensor_id`/`sensor_type` into the envelope, and stores the rest as the
   measured `payload`. It is the MQTT-fed twin of `DHTSensor`.
4. **paho's loop runs on the bridge thread** via `client.loop()` (not
   `loop_start`), so the MQTT callbacks and the SQLite insert share one thread,
   as sqlite3 requires. The loop wakes every second to check `stop_event`.
5. **Nothing else changes.** The forwarder's readings stream uploads these rows
   to `detections2` as it does the DHT's, separated in the cloud by `sensor_id`.
   Gated by `ENABLE_MQTT` in `main.py`, off by default.

## Consequences

- Adding another networked sensor is just another `sensors/<name>` publisher;
  the `sensors/+` subscription and the `readings` table already handle it.
- The Pi is the hub: broker + bridge must be running for networked-sensor data
  to be captured. Acceptable -- the Pi is the always-on node.
- Durability begins once the bridge writes to SQLite. A message lost in the air
  before the bridge sees it is not recovered (the board does not buffer); a
  board could publish QoS 1 for stronger guarantees. Out of scope here.
- The open broker is safe only on the closed hotspot; a shared network would
  require auth/TLS.
