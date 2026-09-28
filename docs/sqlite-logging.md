# SQLite Logging: Change Walkthrough

Review guide for the `sqlite-logging` branch (6 commits after `cebbe45`).
The design rationale is in [ADR 0001](adr/0001-event-pipeline.md); this
document explains **what the code does and where**, in the order it's best
read.

## TL;DR

- Detection logging moved from TinyDB to SQLite (`logs/events.db`).
- Instead of logging only the first sighting, each tracked object now produces
  an **`enter`** event and an **`exit`** event (after 2 s unseen).
- A dedicated **writer thread** inserts each event as soon as it arrives. There
  is no batching.
- Shutdown uses a **sentinel** so the final exit events are always written.
- A **`tracks` view** turns enter/exit pairs into one row per visit, which is
  what a future agent will query.
- Paths now work from any directory, and the app refuses to start if the DB
  can't be opened.

## Big picture

### Data flow

```
 CameraHandler            InferenceHandler                 EventWriter          SQLite
 (camera thread)          (inference thread)               (writer thread)      logs/events.db
 ───────────────          ──────────────────               ───────────────      ──────────────
 read frame ──latest──▶   model.track(frame)
                          _process_detections()
                            new track ID? ──enter──┐
                          _sweep_exits()           │
                            unseen > 2s? ──exit────┤
                                                   ▼
                                           queue.Queue ──▶  queue.get()  ──▶  INSERT + COMMIT
```

The main thread runs the GUI loop and owns startup and shutdown.

### Startup order (`main.py` → `run()`)

1. Start the **writer** thread. It opens the DB inside its own thread.
2. `wait_until_ready()`: if the DB failed to open, **raise and stop here**.
3. Start the **camera** and **inference** threads.
4. Run the GUI loop until 'q' or Ctrl+C.

### Shutdown order (`main.py` → `shutdown()`)

1. `stop_event.set()`
2. Join the camera thread, then the inference thread. On its way out, inference
   emits `exit` for every track still in view.
3. `event_writer.stop()` puts the `None` sentinel on the queue.
4. Join the writer. It writes everything ahead of the sentinel, closes the DB,
   and returns.

**Why this order matters:** if the writer stopped on `stop_event` like the
other threads, it could exit before inference puts its final exits on the
queue, and those events would be lost. The queue is first in, first out, so a
sentinel placed *after* the producers finish guarantees nothing is lost.

## The data

### Event format (one dict, shared by every consumer)

```python
{
    "ts": "2026-09-28T04:38:15.123Z",   # ISO 8601 UTC
    "sensor_id": "S1",                  # device; also the DynamoDB partition key
    "run_id": "2026-09-28T04:30:00.000Z",  # program start time
    "track_id": 7,
    "event_type": "enter",              # or "exit"
    "class_name": "person",
    "confidence": 0.91,
    "x1": 120.0, "y1": 40.0, "x2": 310.0, "y2": 470.0,
}
```

All values are plain Python types, not numpy, so the same dict can go to
SQLite now and to MQTT/JSON later.

`run_id` is needed because **tracker IDs restart at 1 every run**. The pair
`(run_id, track_id)` is what uniquely identifies a track.

### Tables

- **`events`**: append-only log, one row per enter/exit. The code only ever
  inserts rows; it never updates or deletes them.
- **`tracks`** (a view, which is a saved query, not stored data): one row per
  visit, with `entered_at` and `exited_at`. `exited_at IS NULL` means the
  object is still present.

## Commit-by-commit walkthrough

Read these in order. Each commit builds on the previous one.

### 1. `8511ed8` feat(db): add SQLite handler with events table and tracks view

**Files:** [src/sqlite_handler.py](../src/sqlite_handler.py), `.gitignore`

- `SQLiteHandler` handles **storage only**. It knows nothing about threads or
  queues.
- `PRAGMA journal_mode=WAL`: lets another reader (a future agent process) query
  while the writer inserts.
- `PRAGMA synchronous=NORMAL`: safe with WAL, and avoids a disk sync on every
  commit, which is slow on an SD card.
- `insert_event()` commits after every insert, so readers see each event
  immediately.
- The `CHECK (event_type IN ('enter','exit'))` constraint makes the DB itself
  reject bad data.
- `.gitignore` excludes `logs/*.db` and the `-wal` / `-shm` files that WAL mode
  creates alongside the DB.

**Check yourself:** why is `EVENT_COLUMNS` a fixed tuple instead of using
whatever keys the dict happens to have? (Hint: what happens if an event is
missing a field?)

### 2. `14f0058` feat(logging): add event writer thread

**Files:** [src/threads/event_writer.py](../src/threads/event_writer.py)
(replaces `src/threads/logging.py`)

- `task_writer()` opens `SQLiteHandler` **inside the thread**. A `sqlite3`
  connection can only be used by the thread that created it, and `__init__`
  runs on the main thread.
- It loops on a blocking `queue.get()` until it receives `STOP` (`None`).
- A bad event is printed as `[DB] dropped event ...` and skipped, so one bad
  event doesn't stop all logging.
- `finally: db.close()` runs even if something unexpected happens.
- The file was renamed from `logging.py` so it isn't confused with Python's
  standard `logging` module.

**Check yourself:** why is a blocking `get()` with no timeout safe here, when
the inference thread needed `wait(timeout=0.1)` to avoid hanging?

### 3. `bd7300a` fix(db): pair each enter with its next exit in tracks view

**Files:** [src/sqlite_handler.py](../src/sqlite_handler.py) (the `SCHEMA` string)

- **Bug found in testing:** the tracker can bring back a lost ID after we have
  already emitted its exit. The original `LEFT JOIN` matched *every* enter with
  *every* exit for that ID, so 2 enters × 2 exits gave 4 rows instead of 2.
- **Fix:** a subquery picks the **first exit inserted after** each enter
  (`x.id > e.id ORDER BY x.id LIMIT 1`).
- The view is now dropped and recreated on every startup. Views store no data,
  so this is always safe, and existing databases pick up view changes.

**Check yourself:** why compare `id` rather than `ts` to decide which exit
comes "after"? (Hint: look at the test output, where many events share the
same millisecond.)

### 4. `8383e56` feat(inference): emit enter/exit events with plain Python types

**Files:** [src/threads/object_detection.py](../src/threads/object_detection.py)

The loop body is split into small methods:

| Method | Job |
|---|---|
| `_process_detections()` | Converts numpy values to Python types; sends `enter` for new IDs; updates the latest state for known IDs |
| `_sweep_exits()` | Runs **every frame**, including frames with no detections; finds tracks unseen for more than `EXIT_TIMEOUT_S` |
| `_emit_exit()` | Removes the track from `tracked_objects` and sends `exit` |
| `_emit()` | Builds the event dict and puts it on the queue |

Details worth noticing:

- **Two clocks.** `last_seen_ts` is wall-clock time, stored in the DB.
  `last_seen_mono` comes from `time.monotonic()` and is used only to measure
  the timeout. The monotonic clock never jumps when the system time is
  corrected, which happens on a Pi when it syncs its clock after boot.
- **Exit timestamp = last sighting**, not the moment the timeout fired.
  Otherwise every exit would be recorded 2 s late.
- **The class is fixed at first sighting.** YOLO sometimes changes an object's
  class from one frame to the next, and this prevents a "person" from exiting
  as a "car".
- **Final exits on shutdown:** after the `while` loop ends, every remaining
  track gets an exit event.
- **Memory is bounded:** `tracked_objects` only holds what is currently in
  view. Previously it grew forever.

**Check yourself:** what happens to `tracked_objects` and the DB if you set
`EXIT_TIMEOUT_S` to 0.1? What about 60?

### 5. `b58a2da` feat: wire event writer into main and remove TinyDB

**Files:** [src/main.py](../src/main.py), docs, README;
deletes `src/tinydb_handler.py`

- `run_id` is created once in `main` and passed to inference.
- `run()` now starts the writer, calls `_main_loop()` (the old loop body,
  unchanged), and always calls `shutdown()` in a `finally` block.
- **Ctrl+C now triggers a clean shutdown.** Previously it skipped cleanup
  entirely, which matters when running headless on the Pi.
- ADR 0001 gained point 7 describing the sentinel shutdown.

**Check yourself:** follow `shutdown()` line by line and describe what each
thread is doing at each step.

### 6. `183f0a8` fix: resolve paths from project root and fail fast on DB errors

**Files:** [src/main.py](../src/main.py),
[src/threads/event_writer.py](../src/threads/event_writer.py),
[src/sqlite_handler.py](../src/sqlite_handler.py)

- **Bug:** `python src/main.py` run from the repo root crashed with
  `unable to open database file`. Paths like `../logs` are relative to the
  **directory you launch from**, so they pointed outside the project. The model
  path also silently resolved to `~/Desktop/models`.
- **Fix:** `PROJECT_ROOT = Path(__file__).resolve().parent.parent`, and every
  path is built from it.
- `SQLiteHandler` creates `logs/` if it's missing.
- **Fail fast:** the writer sets a `ready` event (and `error`, if it failed).
  `main` waits on it and raises *before* starting the camera and inference.
  Previously the writer died, detection kept running, and every event was lost.

**Check yourself:** why can't `main` simply open the DB itself to check that it
works, instead of waiting on the writer thread?

## Concepts to take away

| Concept | Where it appears |
|---|---|
| Producer/consumer with a queue | inference → queue → writer |
| Sentinel ("poison pill") shutdown | `EventWriter.stop()` / `STOP` |
| Thread-owned resources | SQLite connection opened in `task_writer()` |
| Append-only log + derived view | `events` table + `tracks` view |
| WAL mode for concurrent readers | `SQLiteHandler.__init__` |
| Monotonic vs. wall-clock time | `_process_detections()` / `_sweep_exits()` |
| Fail fast instead of losing data silently | `wait_until_ready()` |
| Paths anchored to the code's own location | `PROJECT_ROOT` in `main.py` |

## Try it yourself

```bash
python src/main.py          # walk in, leave >2s, come back, press 'q'
sqlite3 logs/events.db "SELECT * FROM tracks"
sqlite3 logs/events.db "SELECT * FROM tracks WHERE exited_at IS NULL"   # should be empty after a clean quit
sqlite3 logs/events.db "SELECT event_type, COUNT(*) FROM events GROUP BY 1"
```

Things to try:

- Press Ctrl+C instead of 'q'. The shutdown should be just as clean.
- Temporarily change `DB_PATH` to `/proc/nope/events.db`. The app should
  refuse to start with a clear error.
- While the app runs, query the DB from a second terminal. Thanks to WAL, both
  should work at the same time.

## Known limitations (follow-ups, not bugs in this branch)

1. **Exits only fire while frames arrive.** `_sweep_exits()` runs once per
   inference frame, so if the camera stops delivering frames, exits wait until
   shutdown. Their timestamps are still correct.
2. **`ts` is when inference finished, not when the frame was captured.** It may
   be 100–300 ms late on the Pi. The fix is to capture the timestamp in the
   camera thread along with the frame.
3. **`EXIT_TIMEOUT_S = 2.0` hasn't been tuned** on the real camera or the Pi.
4. **Commented-out TinyDB / Experiment 1 code** is still in `main.py`. Delete
   it in a `chore:` commit; git history keeps a copy.
5. **`ncnn_export.py`** still uses paths relative to `src/`.
6. **Crash leaves open tracks.** If the process is killed (not 'q' or Ctrl+C),
   the last enters have no exit. The `tracks` view shows them as
   `exited_at IS NULL` under an old `run_id`, so a future agent should treat
   only the latest `run_id` as "live".
