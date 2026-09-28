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
All scripts use paths relative to `src/`, so run from there:

```bash
source .venv/bin/activate
cd src
python ncnn_export.py   # one-time: models/yolo11n.pt -> models/yolo11n_ncnn_model/
python main.py          # press 'q' in the preview window to quit
```

- Python 3.12 venv in `.venv/` (ultralytics, opencv-python, ncnn, paho-mqtt 2.x, tinydb).
- There is no test suite. Quick sanity check without a camera:
  `python -m py_compile src/*.py src/threads/*.py`
- `main.py` needs a real camera at `/dev/video0` (`src=0`) and a display
  unless `HEADLESS = True`.

## Architecture
Three threads coordinated with `threading.Event`s, wired together in `src/main.py`:

| Component | File | Role |
|---|---|---|
| `RasPiDeploy` | `src/main.py` | Owns events + queue, starts threads, runs the GUI loop on the main thread (OpenCV `imshow` must run there) |
| `CameraHandler` | `src/threads/camera.py` | Producer: reads frames, stores the latest, sets `camera_frame_ready` |
| `InferenceHandler` | `src/threads/object_detection.py` | Consumer: `model.track(persist=True)` on the latest frame, stores plotted frame, pushes one event per *new* track ID into `queue.Queue` |
| `GUIHandler` | `src/gui_handler.py` | Draws FPS and shows the plotted frame |
| `Logging` | `src/threads/logging.py` | **Stub** — meant to drain the queue into the DB and AWS |
| `DB_Handler` | `src/tinydb_handler.py` | TinyDB wrapper (being replaced by `src/sqlite_handler.py`, currently empty) |
| `AWSPublisher` | `src/aws_publisher.py` | paho-mqtt client, mutual TLS to IoT Core on 8883, QoS 1 |

Key design points:
- "Latest frame wins": threads share the most recent frame via an attribute,
  not a queue, so inference never falls behind the camera.
- Detection events go through a `queue.Queue` so slow I/O (disk, network)
  never blocks inference.
- Events are de-duplicated by tracker ID: only the first sighting is logged.
- `sensor_id` is added to every MQTT payload because it is the DynamoDB
  partition key.

## Architecture decisions
Recorded as ADRs in `docs/adr/` (numbered, one decision per file). Read them
before changing the pipeline, and add a new ADR for any new architectural
decision rather than editing an accepted one.
- `0001-event-pipeline.md`: queue + dedicated logging thread, events written
  as they arrive, enter/exit track events, SQLite in WAL mode; event bus planned.

## Current state (in progress)
- Migrating logging from TinyDB to SQLite; the logging thread isn't started yet.
- AWS publishing is commented out in `main.py`. The endpoint belongs in `.env`
  (`python-dotenv` is installed but not yet used).
- `logs/camera_logs.json` is sample data from Experiment 1 (old event schema:
  `{timestamp, detections: [{label, confidence}]}`).
- Queue events contain numpy types (`conf`, `box`), which must be converted to
  plain Python types before JSON/DB serialization.

## Conventions
- Match the existing style: classes per component, `task_*` methods as thread
  targets, shared `threading.Event`s passed in through constructors.
- Put new thread workers in `src/threads/`.
- Secrets live in `certs/` and `.env`. Never read, print, or commit them.
- `aws-iot-device-sdk-python-v2/` is a vendored, gitignored SDK the code doesn't
  use. Ignore it when searching.
