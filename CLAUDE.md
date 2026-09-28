# IoT Lab — Edge Object Detection

A Raspberry Pi (developed on an x86 laptop) captures USB camera frames, runs
YOLO11n object detection + tracking, shows an annotated preview window, and
logs detection events locally and to AWS IoT Core (MQTT → IoT Rule → DynamoDB).

## Working with me (pair programming)
- I'm learning the architecture as we build. Before a non-trivial change,
  explain the design choice and the alternative you rejected, in a few lines.
- Keep changes small and reviewable; one concern per change.
- Don't commit unless I ask. Don't touch `certs/`, `.env`, or AWS config.

## Run
`main.py` resolves its paths from its own location, so it runs from any
directory. `ncnn_export.py` still uses paths relative to `src/`:

```bash
source .venv/bin/activate
cd src
python ncnn_export.py   # one-time: models/yolo11n.pt -> models/yolo11n_ncnn_model/
python main.py          # press 'q' in the preview window to quit
```

- Python 3.12 venv in `.venv/` (ultralytics, opencv-python, ncnn, paho-mqtt 2.x, requests; SQLite from the stdlib).
- `python src/detection_assistant.py` runs the Ollama Q&A assistant as a
  separate process (needs Ollama + the `MODEL` it names).
- There is no test suite. Quick sanity check without a camera:
  `python -m py_compile src/*.py src/threads/*.py`
- `main.py` needs a real camera at `/dev/video0` (`src=0`) and a display
  unless `HEADLESS = True`.

## Architecture
Five threads (camera, inference, event writer, AWS forwarder, main/GUI) coordinated with `threading.Event`s and a queue, wired together in `src/main.py`:

| Component | File | Role |
|---|---|---|
| `RasPiDeploy` | `src/main.py` | Owns events + queue, starts threads, runs the GUI loop on the main thread (OpenCV `imshow` must run there) |
| `CameraHandler` | `src/threads/camera.py` | Producer: reads frames, stores the latest, sets `camera_frame_ready` |
| `InferenceHandler` | `src/threads/object_detection.py` | Consumer: `model.track(persist=True)` on the latest frame, stores plotted frame, pushes `enter`/`exit` events per track into `queue.Queue` |
| `GUIHandler` | `src/gui_handler.py` | Draws FPS and shows the plotted frame |
| `EventWriter` | `src/threads/event_writer.py` | Consumer: drains the queue into SQLite, one insert per event; stops on a `None` sentinel |
| `SQLiteHandler` | `src/sqlite_handler.py` | Storage only: `events` table, `tracks` view, `consumer_offsets` cursors, WAL mode. Must be created inside the thread that uses it |
| `AWSForwarder` | `src/threads/aws_forwarder.py` | Store-and-forward: reads events after its cursor from SQLite, publishes in batches, advances the cursor only when the whole batch is acked |
| `detection_assistant` | `src/detection_assistant.py` | **Separate process.** Read-only SQLite queries (the only code that reads data) + Ollama model that only routes questions to them and phrases results |
| `AWSPublisher` | `src/aws_publisher.py` | paho-mqtt client, mutual TLS to IoT Core on 8883, QoS 1. Connects in the background; tracks acks for `wait_for_acks()` |

Key design points:
- "Latest frame wins": threads share the most recent frame via an attribute,
  not a queue, so inference never falls behind the camera.
- Detection events go through a `queue.Queue` so slow I/O (disk, network)
  never blocks inference.
- Events are per track, not per frame: `enter` on first sighting, `exit` after
  `EXIT_TIMEOUT_S` unseen. Events are plain Python dicts (ADR 0001 format).
- SQLite is the source of truth; the cloud is a copy (ADR 0002). Delivery is
  at-least-once, so payloads carry an idempotent `event_key` (DynamoDB sort
  key; `sensor_id` is the partition key).

## Architecture decisions
Recorded as ADRs in `docs/adr/` (numbered, one decision per file). Read them
before changing the pipeline, and add a new ADR for any new architectural
decision rather than editing an accepted one.
- `0001-event-pipeline.md`: queue + dedicated logging thread, events written
  as they arrive, enter/exit track events, SQLite in WAL mode.
- `0002-store-and-forward.md`: AWS upload via a per-consumer cursor over the
  SQLite log (replaces 0001's in-memory fan-out plan for option C).
- `0003-detection-assistant.md`: assistant as a separate read-only process;
  model routes and phrases, code computes; counts are visits.

## Current state (in progress)
- Events go to `logs/events.db` (gitignored). Inspect with
  `sqlite3 logs/events.db "SELECT * FROM tracks"`.
- Shutdown order in `main.py` matters: producers join first, then the writer
  sentinel, then the forwarder's final upload, then `publisher.close()`.
- AWS config: `ENABLE_AWS` / `AWS_CLIENT_ID` in `main.py`, `AWS_ENDPOINT` in
  `.env`, certs in `certs/`. Cloud-side setup: `docs/aws-setup.md`.
- Check upload progress: `sqlite3 logs/events.db "SELECT * FROM consumer_offsets"`.
- `logs/camera_logs.json` is sample data from Experiment 1 (old event schema:
  `{timestamp, detections: [{label, confidence}]}`).

## Conventions
- Match the existing style: classes per component, `task_*` methods as thread
  targets, shared `threading.Event`s passed in through constructors.
- Put new thread workers in `src/threads/`.
- Secrets live in `certs/` and `.env`. Never read, print, or commit them.
- `aws-iot-device-sdk-python-v2/` is a vendored, gitignored SDK the code doesn't
  use. Ignore it when searching.
