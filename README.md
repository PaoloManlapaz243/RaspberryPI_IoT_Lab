# IoT Lab: Edge Object Detection

A USB camera feeds YOLO11n detection and tracking. Each object's arrival and
departure is logged to SQLite (`logs/events.db`) and uploaded to AWS IoT Core →
DynamoDB. A separate assistant answers plain-English questions about the log
using a local model via Ollama.

Design decisions are in [`docs/adr/`](docs/adr/).

## 1. Setup

```shell
python3 -m venv .venv
source .venv/bin/activate
pip install opencv-python ultralytics ncnn paho-mqtt python-dotenv requests
```

Export the model once (from `src/`):

```shell
cd src
python ncnn_export.py
```

## 2. AWS (optional)

- Put `AWS_ENDPOINT=...` in `.env` and the device certs in `certs/`. Both are
  gitignored.
- Create the DynamoDB table, IoT Rule, and policy from
  [`docs/aws-setup.md`](docs/aws-setup.md).
- To run without AWS, set `ENABLE_AWS = False` in `src/main.py`.

## 3. Run the detector

```shell
python src/main.py        # press 'q' (or Ctrl+C) to quit
sqlite3 logs/events.db "SELECT * FROM tracks"
```

## 4. Run the assistant (separate terminal)

Install [Ollama](https://ollama.com) (check its site for the current install
steps), then pull the model named by `MODEL` in `src/detection_assistant.py`:

```shell
ollama pull gemma2:2b
python src/detection_assistant.py
```

Try: "is anyone there?", "how many people came by in the last hour?",
"when did you last see a dog?"
