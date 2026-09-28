import sqlite3
from pathlib import Path

# Columns every event dict must provide (see docs/adr/0001-event-pipeline.md)
EVENT_COLUMNS = (
    "ts", "sensor_id", "run_id", "track_id", "event_type",
    "class_name", "confidence", "x1", "y1", "x2", "y2",
)

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

        #Rows come back as sqlite3.Row, so fetch_events_after() can build dicts
        self.conn.row_factory = sqlite3.Row

        self.conn.executescript(SCHEMA)

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
