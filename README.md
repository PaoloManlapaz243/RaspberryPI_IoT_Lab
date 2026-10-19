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
pip install -e ".[dev]"     # the iot_lab package, its dependencies, and pytest
```

`-e` (editable) links the install to `src/`, so code edits take effect without
reinstalling. Export the model once:

```shell
python -m iot_lab.ncnn_export
```

## 2. AWS (optional)

- Put `AWS_ENDPOINT=...` in `.env` and the device certs in `certs/`. Both are
  gitignored.
- Create the DynamoDB table, IoT Rule, and policy from
  [`docs/aws-setup.md`](docs/aws-setup.md).
- To run without AWS, set `ENABLE_AWS = False` in `src/iot_lab/main.py`.

## 3. DHT11 sensor (Raspberry Pi, optional)

Wire the module's DATA pin to GPIO4 (physical pin 7), VCC to 3.3 V, and GND to
ground; a bare 4-pin sensor also needs a 10k pull-up between DATA and VCC. Then:

```shell
sudo apt install swig python3-dev liblgpio-dev
pip install -e ".[pi]"
```

and set `ENABLE_DHT = True` in `src/iot_lab/main.py`. Readings are logged to the
`readings` table and uploaded alongside detections.

## 4. Run the detector

```shell
python -m iot_lab.main    # press 'q' (or Ctrl+C) to quit
sqlite3 logs/events.db "SELECT * FROM tracks"
sqlite3 logs/events.db "SELECT * FROM readings ORDER BY id DESC LIMIT 5"
```

## 5. Run the assistant (separate terminal)

Install [Ollama](https://ollama.com) (check its site for the current install
steps), then pull the model named by `MODEL` in `src/iot_lab/detection_assistant.py`:

```shell
ollama pull gemma2:2b
python -m iot_lab.detection_assistant
```

Try: "is anyone there?", "how many people came by in the last hour?",
"when did you last see a dog?"

How a question becomes a reply: [`docs/assistant-flow.md`](docs/assistant-flow.md).

## 6. Tests and evals

```shell
pytest                                      # unit tests: fast, no Ollama or camera
python evals/eval_assistant.py --held-out   # routing score: needs Ollama, takes minutes
```

Why they're separate: [ADR 0008](docs/adr/0008-project-layout.md).
