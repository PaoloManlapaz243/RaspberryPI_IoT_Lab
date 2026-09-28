# ADR 0002: Store-and-forward upload to AWS IoT Core

- **Status:** Accepted
- **Date:** 2026-09-28
- **Supersedes:** the "Future direction: Option C" fan-out plan in
  [ADR 0001](0001-event-pipeline.md) (fan out from the durable log, not from
  in-memory queues). The rest of ADR 0001 still applies.

## Context

Detection events are stored in SQLite (ADR 0001) and must also reach AWS IoT
Core → IoT Rule → DynamoDB. The device is an edge node with unreliable
connectivity, and reboots or power loss must not lose cloud data.

paho-mqtt's `publish()` never blocks, but while offline it holds unsent
messages **in RAM**. They are lost on restart and nothing re-sends them, even
though SQLite still has them.

## Options considered

| Option | Summary | Verdict |
|---|---|---|
| **1. EventWriter also publishes** | Insert, then `publish()` in the same thread | Simplest, but not durable: offline messages live in paho's RAM queue and are lost on reboot. SQLite and the cloud can disagree. |
| **2. Separate consumer per destination (in-memory fan-out)** | Inference feeds two queues/threads: SQLite and MQTT | More moving parts than 1, and **the same durability flaw**: the MQTT queue is still RAM. |
| **3. Store-and-forward (transactional outbox)** | SQLite is the source of truth; a forwarder thread reads unsent rows and publishes them, tracking a per-consumer cursor | **Chosen.** Survives outages and reboots, and SQLite and the cloud can't diverge. |

## Decision

```
inference → queue → EventWriter → SQLite (source of truth)
                         │ inserted.set()
                         ▼
                  AWSForwarder: fetch rows after cursor → publish batch
                                → all acked? → advance cursor in consumer_offsets
```

1. **SQLite is the single source of truth.** The cloud gets a copy of the log.
2. **One cursor per consumer** in `consumer_offsets(consumer, last_event_id)`.
   Future consumers (for example the agent trigger) each get their own cursor
   into the same log. This replaces ADR 0001's "each consumer gets its own
   in-memory queue".
3. **Batches, stop-and-wait.** Publish up to 50 rows, wait for all QoS 1 acks,
   and only then advance the cursor. Acks can arrive out of order, so
   per-message cursors would need gap tracking; per-batch is simple and
   correct.
4. **Publish only while connected.** SQLite is the queue, so paho's RAM queue
   only ever holds the in-flight batch.
5. **An in-flight batch is not re-sent from SQLite within a run.** paho re-sends
   it after a reconnect; the forwarder waits for its acks. SQLite re-reads
   happen after a restart.
6. **The writer wakes the forwarder** with a `threading.Event` after each
   insert, so uploads happen within milliseconds. A 1 s poll also flushes the
   backlog after a reconnect.
7. **Shutdown:** stop producers → writer sentinel → forwarder makes a final
   bounded (5 s) upload attempt → `publisher.close()`. Anything left is sent
   next run.
8. **Config errors fail fast; network outages don't.** `ENABLE_AWS = True` with
   a missing `AWS_ENDPOINT` or cert means the app refuses to start. With no
   network, it runs and logs locally.

## Consequences

- **Delivery is at-least-once.** A crash between the broker's ack and the
  cursor update re-sends that batch. The cloud side must be **idempotent**:
  every payload carries `event_key = "<ts>#<run_id>#<track_id>#<event_type>"`,
  built only from stored columns, so a re-sent event overwrites itself in
  DynamoDB. See [docs/aws-setup.md](../aws-setup.md) for the table keys.
- Stop-and-wait holds newer events for about one round trip (~50–100 ms) while
  a batch is in flight. That's fine at detection-event rates.
- A second SQLite connection (the forwarder's own); WAL mode makes that safe.
  The schema script now runs in one transaction so both threads can open the
  DB concurrently.
- `consumer_offsets` is a new table. `CREATE TABLE IF NOT EXISTS` adds it to
  existing databases, so no migration is needed.
- On first run against an existing `events.db`, the forwarder uploads the whole
  existing backlog (its cursor starts at 0).
