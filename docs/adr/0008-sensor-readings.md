# ADR 0008: Periodic sensor readings as a second stream

- **Status:** Accepted
- **Date:** 2026-10-12
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
   (`{"temperature_c": 24.0, "humidity": 65}`). A new sensor type needs **no
   schema change** — only a new `sensor_type` and whatever keys it puts in
   `payload`. This is the future-proofing: the shape that varies is quarantined
   inside one TEXT column, while the columns the system queries on
   (`ts`, `sensor_id`, `sensor_type`) stay fixed and typed.

2. **The forwarder becomes multi-stream.** It drives a list of streams, each
   with its own `consumer_offsets` cursor and its own payload builder, and
   drains them with the same batch/stop-and-wait logic as ADR 0002. Detections
   are one stream; readings are another. Streams are drained one at a time so
   the publisher's global ack-wait stays unambiguous.

3. **Readings reuse the existing MQTT path** (same publisher, topic, IoT Rule,
   `detections2` table). They are separated in the cloud by `sensor_id` (the
   partition key), exactly as detections are. Their idempotent sort key is
   `event_key = "<ts>#<sensor_id>#<sensor_type>"`, built only from stored
   fields, so a re-sent reading overwrites itself (at-least-once, per ADR 0002).

4. **Readings flatten to top-level fields on publish**, like detections, so the
   DynamoDBv2 rule maps each measured value to its own attribute. (SQLite keeps
   the values packed in `payload`; the reading payload builder unpacks them.)

5. **A DHT producer thread** (`src/threads/dht_sensor.py`) reads the sensor
   every N seconds and pushes reading dicts onto a readings queue, drained into
   SQLite by a generic queue writer — mirroring the camera → queue → writer
   path, so slow sensor I/O never blocks anything else.

## Consequences

- Adding a future sensor is local: a producer thread (or an MQTT subscriber)
  that writes `readings` rows, and — if it needs its own cloud cadence — one
  more stream descriptor in the forwarder. No change to detections.
- `readings` and `consumer_offsets` rows for the new consumer are created with
  `CREATE TABLE IF NOT EXISTS` / first-run cursor = 0: no migration.
- The generic `QueueWriter` replaces the detection-specific `EventWriter`;
  behavior for detections is identical (same insert, same sentinel shutdown).
- The cloud `detections2` table now holds two row shapes (detections and
  readings). It is schemaless for non-key attributes, so they coexist; tell
  them apart by `sensor_id` or the presence of `sensor_type`.
