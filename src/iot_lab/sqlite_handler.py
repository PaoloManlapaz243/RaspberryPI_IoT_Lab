import json
import sqlite3
from pathlib import Path

# Columns every detection event dict must provide (see docs/adr/0001-event-pipeline.md)
EVENT_COLUMNS = (
    "ts", "sensor_id", "run_id", "track_id", "event_type",
    "class_name", "confidence", "x1", "y1", "x2", "y2",
)

# Columns every sensor reading dict must provide (see docs/adr/0009-sensor-readings.md).
# `payload` is a dict of the measured values; it is stored as a JSON string.
READING_COLUMNS = ("ts", "sensor_id", "sensor_type", "payload")

#Wrapped in one transaction: the writer and forwarder threads both open the DB
#at startup, and interleaved DROP/CREATE VIEW statements would otherwise fail
SCHEMA = """
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    ts          TEXT    NOT NULL,
    sensor_id   TEXT    NOT NULL,
    run_id      TEXT    NOT NULL,
    track_id    INTEGER NOT NULL,
    event_type  TEXT    NOT NULL CHECK (event_type IN ('enter', 'exit')),
    class_name  TEXT    NOT NULL,
    confidence  REAL    NOT NULL,
    x1 REAL, y1 REAL, x2 REAL, y2 REAL
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_track ON events(sensor_id, run_id, track_id);

-- Periodic scalar sensor readings (docs/adr/0009-sensor-readings.md).
-- Generic by design: the per-sensor shape lives in the JSON `payload` column,
-- so a new sensor type needs no schema change -- only a new sensor_type value.
-- payload holds measured values only (derived ones, like Fahrenheit, are
-- computed when read), and must be a JSON object.
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY,
    ts          TEXT    NOT NULL,
    sensor_id   TEXT    NOT NULL,
    sensor_type TEXT    NOT NULL,
    payload     TEXT    NOT NULL CHECK (json_valid(payload) AND json_type(payload) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);
CREATE INDEX IF NOT EXISTS idx_readings_sensor ON readings(sensor_id, sensor_type);

-- Views hold no data, so always recreate them to match this code
DROP VIEW IF EXISTS tracks;

-- One row per visit: when it appeared, when it left (NULL = still present).
-- The tracker can revive an ID after we emitted its exit, so one track_id may
-- have several enter/exit pairs; pair each enter with the NEXT exit (by id,
-- which is insertion order) rather than joining every enter to every exit.
CREATE VIEW tracks AS
SELECT e.sensor_id, e.run_id, e.track_id, e.class_name,
       e.ts AS entered_at,
       (SELECT x.ts FROM events x
        WHERE x.sensor_id = e.sensor_id AND x.run_id = e.run_id
          AND x.track_id = e.track_id AND x.event_type = 'exit'
          AND x.id > e.id
        ORDER BY x.id LIMIT 1) AS exited_at
FROM events e
WHERE e.event_type = 'enter';

-- Store-and-forward cursors: each consumer (e.g. the AWS forwarder) records
-- the highest events.id it has fully delivered, and resumes after it
CREATE TABLE IF NOT EXISTS consumer_offsets (
    consumer      TEXT    PRIMARY KEY,
    last_event_id INTEGER NOT NULL
);

COMMIT;
"""


class SQLiteHandler:
    """
    Storage only: no threading or queue logic lives here.
    sqlite3 connections belong to the thread that created them, so construct
    this INSIDE the thread that uses it. Each thread (writer, forwarder) gets
    its own instance; WAL mode lets them work concurrently.
    """

    def __init__(self, filepath: str):
        #sqlite3 creates the file but not its folder
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(filepath)

        #WAL lets readers (e.g. an agent process) query while we write
        self.conn.execute("PRAGMA journal_mode=WAL")
        #Safe with WAL; skips an fsync per commit (matters on an SD card)
        self.conn.execute("PRAGMA synchronous=NORMAL")

        #Rows come back as sqlite3.Row, so fetch_*_after() can build dicts
        self.conn.row_factory = sqlite3.Row

        self.conn.executescript(SCHEMA)

    # ---------- detection events ----------

    def insert_event(self, event: dict):
        #Commit per event so readers see it immediately (no batching)
        values = tuple(event[col] for col in EVENT_COLUMNS)
        placeholders = ", ".join("?" for _ in EVENT_COLUMNS)
        self.conn.execute(
            f"INSERT INTO events ({', '.join(EVENT_COLUMNS)}) VALUES ({placeholders})",
            values,
        )
        self.conn.commit()

    def fetch_events_after(self, last_event_id: int, limit: int) -> list[dict]:
        #Oldest first, so a consumer delivers events in the order they happened
        rows = self.conn.execute(
            f"SELECT id, {', '.join(EVENT_COLUMNS)} FROM events "
            "WHERE id > ? ORDER BY id LIMIT ?",
            (last_event_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---------- sensor readings (ADR 0009) ----------

    def insert_reading(self, reading: dict):
        #`payload` arrives as a dict and is stored as a JSON string
        values = (
            reading["ts"],
            reading["sensor_id"],
            reading["sensor_type"],
            json.dumps(reading["payload"]),
        )
        self.conn.execute(
            f"INSERT INTO readings ({', '.join(READING_COLUMNS)}) VALUES (?, ?, ?, ?)",
            values,
        )
        self.conn.commit()

    def fetch_readings_after(self, last_event_id: int, limit: int) -> list[dict]:
        #Mirrors fetch_events_after. payload is returned as the raw JSON string;
        #the forwarder's reading payload builder unpacks it.
        rows = self.conn.execute(
            f"SELECT id, {', '.join(READING_COLUMNS)} FROM readings "
            "WHERE id > ? ORDER BY id LIMIT ?",
            (last_event_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---------- store-and-forward cursors ----------

    def get_offset(self, consumer: str) -> int:
        #0 = nothing delivered yet (SQLite ids start at 1)
        row = self.conn.execute(
            "SELECT last_event_id FROM consumer_offsets WHERE consumer = ?",
            (consumer,),
        ).fetchone()
        return row["last_event_id"] if row else 0

    def set_offset(self, consumer: str, last_event_id: int):
        self.conn.execute(
            "INSERT INTO consumer_offsets (consumer, last_event_id) VALUES (?, ?) "
            "ON CONFLICT(consumer) DO UPDATE SET last_event_id = excluded.last_event_id",
            (consumer, last_event_id),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
