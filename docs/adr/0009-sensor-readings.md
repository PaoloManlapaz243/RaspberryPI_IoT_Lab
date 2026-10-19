# ADR 0009: Periodic sensor readings as a second stream

- **Status:** Accepted
- **Date:** 2026-10-12 (revised 2026-10-19 while porting to the `iot_lab`
  package: the sensor runs as a thread in `main.py`, not a separate program;
  see Decision 5)
- **Extends:** [ADR 0001](0001-event-pipeline.md) (event log) and
  [ADR 0002](0002-store-and-forward.md) (per-consumer cursor over the log).
  The detection `events` table and `tracks` view are unchanged.

## Context

The system is gaining non-camera sensors (first: a DHT11 temperature/humidity
sensor on the Pi; later, more, including sensors reporting over MQTT). These
produce **periodic scalar readings**, not detection events.

The `events` table is detection-shaped: NOT NULL `track_id`, an `event_type`
`CHECK (… IN ('enter','exit'))`, `class_name`, `confidence`, bbox columns, and
a `tracks` view that pairs enter/exit. A temperature reading has none of those,
and its real values (temperature, humidity) have nowhere to go. Forcing
readings into `events` would violate the CHECK constraint and corrupt the
meaning of the detection columns.

## Options considered

| Option | Summary | Verdict |
|---|---|---|
| **A. Reuse `events`** | Map a reading onto the detection columns (`class_name="dht11"`, `confidence`=temp, fake a `track_id`/`event_type`) | Rejected. The `event_type` CHECK rejects it, it pollutes the `tracks` view, and it destroys column semantics. |
| **B. One generic table for every kind** (envelope + JSON payload for detections too) | Collapse detections and readings into one table | Rejected. Throws away the typed detection schema and the `tracks` view for no gain; detections and readings have genuinely different shapes and queries. |
| **C. A second `readings` table + generalized forwarder** | Detections stay as-is; readings get their own generic table and their own forwarder cursor | **Chosen.** Keeps detection semantics, and makes "add a sensor" a small, local change. |

## Decision

1. **New `readings` table**, generic for any periodic scalar sensor:

   ```
   readings(id, ts, sensor_id, sensor_type, payload)
   ```

   `payload` is a JSON object of the measured values
   (`{"temperature_c": 24.0, "humidity": 65}`), enforced by
   `CHECK (json_valid(payload) AND json_type(payload) = 'object')`. It holds
   **measured values only**: derived values (Fahrenheit) are computed when
   read, so two stored columns can never disagree. A new sensor type needs **no
   schema change** — only a new `sensor_type` and whatever keys it puts in
   `payload`. This is the future-proofing: the shape that varies is quarantined
   inside one TEXT column, while the columns the system queries on
   (`ts`, `sensor_id`, `sensor_type`) stay fixed and typed.

2. **The forwarder becomes multi-stream.** It drives a list of streams, each
   with its own `consumer_offsets` cursor and its own payload builder, and
   drains them with the same batch/stop-and-wait logic as ADR 0002. Detections
   are one stream; readings are another. A stream names its fetch method as a
   function (`SQLiteHandler.fetch_readings_after`), not a string, so a typo
   fails at import. Streams are drained one after another; a timed-out batch
   may still be in flight while the next stream publishes, which is safe
   because the publisher's ack-wait covers every pending message.

3. **Readings reuse the existing MQTT path** (same publisher, topic, IoT Rule,
   `detections2` table). They are separated in the cloud by `sensor_id` (the
   partition key), exactly as detections are. Their idempotent sort key is
   `event_key = "<ts>#<sensor_id>#<sensor_type>"`, built only from stored
   fields, so a re-sent reading overwrites itself (at-least-once, per ADR 0002).

4. **Readings flatten to top-level fields on publish**, like detections, so the
   DynamoDBv2 rule maps each measured value to its own attribute. (SQLite keeps
   the values packed in `payload`; the reading payload builder unpacks them.)

5. **The DHT11 runs as a producer thread inside `main.py`**
   (`src/iot_lab/threads/dht_sensor.py`, `DHTSensor.task_sensor`), enabled by
   `ENABLE_DHT`. It stops on the shared `stop_event` and is joined with the
   other producers, so the forwarder's final drain includes its last readings.
   - **Not a separate program** (the first version): anything that produces
     data for the device's pipeline belongs in the device's main program. A
     separate program still needed `main.py` running for its rows to upload,
     and meant two lifecycles and two shutdowns. The detection assistant is a
     separate process for the opposite reason: it only reads.
   - **Writes directly, not through a queue + writer.** The detection queue
     exists so inference never waits on disk. This loop sleeps seconds between
     reads, so a ~1 ms insert costs it nothing; it opens its own
     `SQLiteHandler` in its thread and sets the forwarder's `wakeup`.
   - **Hardware libraries load only when enabled**, so the laptop never needs
     them. Enabling it without them fails at startup with install steps
     (`pip install -e ".[pi]"`).

## Consequences

- Adding a future sensor is local: a producer thread (or an MQTT subscriber)
  that writes `readings` rows, and — if it needs its own cloud cadence — one
  more stream descriptor in the forwarder. No change to detections.
- `readings` and `consumer_offsets` rows for the new consumer are created with
  `CREATE TABLE IF NOT EXISTS` / first-run cursor = 0: no migration.
- `EventWriter` is unchanged; it still only handles detections.
- Existing databases: `CREATE TABLE IF NOT EXISTS` doesn't add the `payload`
  CHECK to a `readings` table created by the first version. Once its rows are
  uploaded (the `aws_iot_readings` cursor in `consumer_offsets` has reached the
  last id), drop that table and let it be recreated.
- The cloud `detections2` table now holds two row shapes (detections and
  readings). It is schemaless for non-key attributes, so they coexist; tell
  them apart by `sensor_id` or the presence of `sensor_type`.
