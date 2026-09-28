# AWS Setup for Detection Uploads

What the AWS side must look like for the store-and-forward upload
([ADR 0002](adr/0002-store-and-forward.md)) to work.

> **Verify against the AWS console.** Menu names and defaults change over time.
> The *values* below (keys, topic, fields) are what the code depends on; how
> you click through to set them may differ.

## 1. DynamoDB table

| Setting | Value |
|---|---|
| Table name | your choice, e.g. `detection_events` |
| Partition key | `sensor_id` (**String**) |
| Sort key | `event_key` (**String**) |
| Capacity | On-demand is simplest for a lab |

**Why these keys:**

- `sensor_id` groups each device's events together, so one query returns
  everything from a sensor.
- `event_key` = `"<ts>#<run_id>#<track_id>#<event_type>"`, for example
  `2026-09-28T16:36:24.746Z#2026-09-28T16:36:17.708Z#12#exit`.
  - **Unique:** two events never share all four parts.
  - **Stable:** a re-sent event (at-least-once delivery) has the *same* key,
    so it overwrites itself instead of creating a duplicate.
  - **Time-sortable:** it starts with an ISO timestamp, so a range query like
    `event_key BETWEEN "2026-09-28T00" AND "2026-09-29T00"` returns a day's
    events in order.

A sort key of just `ts` would be wrong: events in the same millisecond (common
at shutdown) would overwrite each other.

## 2. IoT Rule

| Setting | Value |
|---|---|
| SQL | `SELECT * FROM 'detections/events'` |
| Action | DynamoDBv2: writes each top-level JSON field as its own attribute |
| Table | the table from step 1 |
| Error action | recommended: send failures to CloudWatch Logs, otherwise failed inserts are invisible |

## 3. Device policy (attached to the `detector-01` certificate)

Must allow at least:

- `iot:Connect` for client ID `detector-01`
- `iot:Publish` to topic `detections/events`

The certificate must be **ACTIVE**, and the policy must be **attached** to it.
These are the usual causes of the `[AWS] CONNECT REFUSED` and
`[AWS] DISCONNECTED ... right after a publish` messages.

## 4. Device files

| What | Where | Used as |
|---|---|---|
| Endpoint | `.env` → `AWS_ENDPOINT=xxxxxxxx-ats.iot.<region>.amazonaws.com` | `os.getenv("AWS_ENDPOINT")` in `main.py` |
| Root CA | `certs/AmazonRootCA1.pem` | `ca_path` |
| Device cert | `certs/detector-01.cert.pem` | `cert_path` |
| Private key | `certs/detector-01.private.key` | `key_path` |

Both `.env` and `certs/` are gitignored. Cert filenames are derived from
`AWS_CLIENT_ID` in `main.py`. To run without AWS, set `ENABLE_AWS = False`.

## 5. Payload the rule receives

```json
{
  "sensor_id": "S1",
  "event_key": "2026-09-28T16:36:24.746Z#2026-09-28T16:36:17.708Z#12#exit",
  "ts": "2026-09-28T16:36:24.746Z",
  "run_id": "2026-09-28T16:36:17.708Z",
  "track_id": 12,
  "event_type": "exit",
  "class_name": "person",
  "confidence": 0.808,
  "x1": 535.7, "y1": 180.3, "x2": 639.7, "y2": 386.6
}
```

## Checking it works

1. Run the app with network, walk in and out of view, then press 'q'.
2. The terminal should show `[AWS] connected OK`, and no `WARNING: closing with
   N unacknowledged`.
3. Check the upload cursor. It should equal the newest event id:
   ```bash
   sqlite3 logs/events.db "SELECT * FROM consumer_offsets; SELECT MAX(id) FROM events;"
   ```
4. The items should appear in the DynamoDB table (Explore items).
5. To test outage recovery: disconnect Wi-Fi, generate events, quit, reconnect,
   and run again. The forwarder should log `resuming after event id N` and
   upload the backlog.
