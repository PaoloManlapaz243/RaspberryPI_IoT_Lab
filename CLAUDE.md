# IoT Lab — Edge Object Detection

A Raspberry Pi (developed on an x86 laptop) captures USB camera frames, runs
YOLO11n object detection + tracking, shows an annotated preview window, reads
a DHT11 temperature/humidity sensor, and logs detection events and sensor
readings locally and to AWS IoT Core (MQTT → IoT Rule → DynamoDB).

## Working with me (pair programming)
- I'm learning the architecture as we build. Before a non-trivial change,
  explain the design choice and the alternative you rejected, in a few lines.
- Keep changes small and reviewable; one concern per change.
- Don't commit unless I ask. Don't touch `certs/`, `.env`, or AWS config.

## Run
The code is the `iot_lab` package in `src/iot_lab/`, installed editable
(`pip install -e ".[dev]"`; dependencies are in `pyproject.toml`). Paths are
anchored to the code's location, so everything runs from any directory:

```bash
source .venv/bin/activate
python -m iot_lab.ncnn_export   # one-time: models/yolo11n.pt -> models/yolo11n_ncnn_model/
python -m iot_lab.main          # press 'q' in the preview window to quit
```

- Python 3.12 venv in `.venv/`. Imports are absolute (`from iot_lab.threads.camera import ...`).
- `python -m iot_lab.detection_assistant` runs the Ollama Q&A assistant as a
  separate process (needs Ollama + the `MODEL` it names).
- `python evals/eval_assistant.py [model] [--held-out] [--no-guard] [--verbose]`
  scores the assistant's routing (accuracy, misroutes to chat, retries,
  latency). Run it before and after any prompt or model change; never tune
  against the held-out set. The chat-feature gate is held-out >= 90% and 0
  misroutes to chat.
- `pytest` runs the unit tests in `tests/` (under a second; no Ollama, camera,
  or network). The assistant tests need `metadata.yaml` and say so if it's
  missing.
- `main.py` needs a real camera at `/dev/video0` (`src=0`) and a display
  unless `HEADLESS = True`.

## Architecture
Up to six threads (camera, inference, event writer, AWS forwarder, DHT sensor, main/GUI) coordinated with `threading.Event`s and a queue, wired together in `src/iot_lab/main.py`:

| Component | File | Role |
|---|---|---|
| `RasPiDeploy` | `src/iot_lab/main.py` | Owns events + queue, starts threads, runs the GUI loop on the main thread (OpenCV `imshow` must run there) |
| `CameraHandler` | `src/iot_lab/threads/camera.py` | Producer: reads frames, stores the latest, sets `camera_frame_ready` |
| `InferenceHandler` | `src/iot_lab/threads/object_detection.py` | Consumer: `model.track(persist=True)` on the latest frame, stores plotted frame, pushes `enter`/`exit` events per track into `queue.Queue` |
| `GUIHandler` | `src/iot_lab/gui_handler.py` | Draws FPS and shows the plotted frame |
| `EventWriter` | `src/iot_lab/threads/event_writer.py` | Consumer: drains the queue into SQLite, one insert per event; stops on a `None` sentinel |
| `SQLiteHandler` | `src/iot_lab/sqlite_handler.py` | Storage only: `events` table, `tracks` view, `readings` table (JSON `payload`), `consumer_offsets` cursors, WAL mode. Must be created inside the thread that uses it |
| `DHTSensor` | `src/iot_lab/threads/dht_sensor.py` | Producer (if `ENABLE_DHT`): reads the DHT11 every 3 s and writes `readings` rows directly (no queue), waking the forwarder |
| `AWSForwarder` | `src/iot_lab/threads/aws_forwarder.py` | Store-and-forward over streams (detections, readings): each reads rows after its own cursor, publishes in batches, advances the cursor only when the whole batch is acked |
| `detection_assistant` | `src/iot_lab/detection_assistant.py` | **Separate process.** Read-only SQLite queries (the only code that reads data) + Ollama model that only routes questions to them and phrases results |
| `AWSPublisher` | `src/iot_lab/aws_publisher.py` | paho-mqtt client, mutual TLS to IoT Core on 8883, QoS 1. Connects in the background; tracks acks for `wait_for_acks()` |

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
- `0004-structured-model-output.md`: model output is constrained (JSON schema),
  validated (reject + one retry, never guess), and measured (eval with a
  held-out set).
- `0005-routing-evaluation.md`: 50/30 eval with safety metrics, `unclear`
  intent, labels validated against the detector's vocabulary, code guard for
  unsupported questions; two-stage routing measured and rejected.
- `0006-label-vocabulary-in-schema.md`: the router's `label` is an enum of the
  detector's labels (+ `not_a_detector_label`, null); the model maps words, with no
  hand-written synonym table.
- `0007-per-function-schema-and-label-check.md`: `ROUTER_SCHEMA` is generated
  per function (`anyOf`, exact args, all present); `route()` = guard question →
  `_ask_model()` → guard answer (literal second label → unclear). The
  chat-feature gate decision is open there.
- `0008-project-layout.md`: installable `iot_lab` package in `src/`,
  unit tests in `tests/` (deterministic, no services), model evals in `evals/`
  (need Ollama, give a score).
- `0009-sensor-readings.md` (Ben): generic `readings` table (envelope columns +
  JSON `payload` of measured values), a multi-stream forwarder, and the DHT11
  as a producer thread in `main.py` that writes directly.

## Current state (in progress)
- Open issues, pending decisions, and next steps: `docs/open-issues.md`.
  Read it when resuming work; remove items in the commit that resolves them.
- Events go to `logs/events.db` (gitignored). Inspect with
  `sqlite3 logs/events.db "SELECT * FROM tracks"`.
- Shutdown order in `main.py` matters: producers join first, then the writer
  sentinel, then the forwarder's final upload, then `publisher.close()`.
- DHT11: `ENABLE_DHT` / `DHT_SENSOR_ID` / `DHT_PIN` in `main.py` (off by default;
  Pi only, `pip install -e ".[pi]"`). Inspect with
  `sqlite3 logs/events.db "SELECT * FROM readings ORDER BY id DESC LIMIT 5"`.
- AWS config: `ENABLE_AWS` / `AWS_CLIENT_ID` in `main.py`, `AWS_ENDPOINT` in
  `.env`, certs in `certs/`. Cloud-side setup: `docs/aws-setup.md`.
- Check upload progress: `sqlite3 logs/events.db "SELECT * FROM consumer_offsets"`.
- `logs/camera_logs.json` is sample data from Experiment 1 (old event schema:
  `{timestamp, detections: [{label, confidence}]}`).

## Conventions
- Match the existing style: classes per component, `task_*` methods as thread
  targets, shared `threading.Event`s passed in through constructors.
- Put new thread workers in `src/iot_lab/threads/`.
- `docs/assistant-flow.md` is a Mermaid flowchart of `route()` / `answer()`.
  Update it in the same commit as any change to that flow.
- Adding an assistant function: update `FUNCTIONS`, `ALLOWED_ARGS` /
  `REQUIRED_ARGS`, the router prompt, eval cases, and check
  `UNSUPPORTED_PATTERNS` (ADRs 0004-0006).
- Tests (ADR 0008): deterministic logic gets a unit test in `tests/`; model
  behaviour gets eval cases in `evals/`. Tests use `tmp_path` for databases
  and replace `_ollama` with canned replies; never touch `logs/events.db`.
- The assistant reads the detector's labels from
  `models/yolo11n_ncnn_model/metadata.yaml`, so run `python -m iot_lab.ncnn_export` first.
- Secrets live in `certs/` and `.env`. Never read, print, or commit them.
- `aws-iot-device-sdk-python-v2/` is a vendored, gitignored SDK the code doesn't
  use. Ignore it when searching.
