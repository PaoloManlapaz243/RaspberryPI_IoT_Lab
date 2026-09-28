import sqlite3

# Columns every event dict must provide (see docs/adr/0001-event-pipeline.md)
EVENT_COLUMNS = (
    "ts", "sensor_id", "run_id", "track_id", "event_type",
    "class_name", "confidence", "x1", "y1", "x2", "y2",
)

SCHEMA = """
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
"""


class SQLiteHandler:
    """
    Storage only: no threading or queue logic lives here.
    sqlite3 connections belong to the thread that created them, so construct
    this INSIDE the thread that will write (the event writer's task).
    """

    def __init__(self, filepath: str):
        self.conn = sqlite3.connect(filepath)

        #WAL lets readers (e.g. an agent process) query while we write
        self.conn.execute("PRAGMA journal_mode=WAL")
        #Safe with WAL; skips an fsync per commit (matters on an SD card)
        self.conn.execute("PRAGMA synchronous=NORMAL")

        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def insert_event(self, event: dict):
        #Commit per event so readers see it immediately (no batching)
        values = tuple(event[col] for col in EVENT_COLUMNS)
        placeholders = ", ".join("?" for _ in EVENT_COLUMNS)
        self.conn.execute(
            f"INSERT INTO events ({', '.join(EVENT_COLUMNS)}) VALUES ({placeholders})",
            values,
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
