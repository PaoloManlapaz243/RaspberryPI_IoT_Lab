"""
Natural-language assistant over your camera detection data.

Design:
  - The functions below are the ONLY code that touches the data. Deterministic,
    testable. This is your "capabilities" set. Breadth comes from operations over
    your fields (label, confidence, timestamp), parameterized so ONE function
    covers a whole family of questions -- not a function per phrasing.
  - The SLM (premade model via Ollama) does TWO things: route (phrasing -> one
    function + args) and phrase (result -> plain English). It never computes.

Run:
  1) Ollama running (Windows: the tray app serves it on :11434 automatically)
  2) ollama pull gemma2:2b
  3) python assistant.py
"""

import json
import time
import uuid
import requests
from datetime import datetime, timedelta
from collections import Counter
from tinydb import TinyDB

# ---------------- config ----------------
OLLAMA_URL   = "http://localhost:11434/api/chat"
MODEL        = "gemma2:2b"         # 2B routes more reliably than 1.5b; qwen2.5:3b is even better
TINYDB_PATH  = "camera_logs.json"  # same file camera.py writes to
USE_LLM_PHRASING = True            # False = skip 2nd model call (faster, less natural)

# chat logging to DynamoDB via MQTT (reuses your IoT cert -- no new credentials).
# A second IoT rule routes topic 'chat/logs' -> chat_logs table.
LOG_CHAT_TO_AWS = True
AWS_ENDPOINT    = "alqw25622p8x0-ats.iot.us-east-2.amazonaws.com"
CA_PATH         = "AmazonRootCA1.pem"
CERT_PATH       = "detector-01.cert.pem"
KEY_PATH        = "detector-01.private.key"
CHAT_TOPIC      = "chat/logs"


# ============ DATA LAYER: the only things that read the DB ============

def _load_events():
    """Open fresh each call to get the latest data. Retry on a partial write
    (camera.py may be mid-write, since it's a separate process)."""
    for _ in range(3):
        try:
            db = TinyDB(TINYDB_PATH)
            data = db.all()
            db.close()
            return data
        except Exception:
            time.sleep(0.05)
    return []

def _within(event, since_minutes):
    if since_minutes is None:
        return True
    try:
        t = datetime.strptime(event["timestamp"], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return False
    return t >= datetime.now() - timedelta(minutes=since_minutes)

def _iter_detections(events, since_minutes):
    for e in events:
        if _within(e, since_minutes):
            for d in e.get("detections", []):
                yield e["timestamp"], d

# ---- capabilities ----

def recent(n=5):
    events = sorted(_load_events(), key=lambda e: e.get("timestamp", ""), reverse=True)
    return {"events": events[:int(n)]}

def count(label=None, since_minutes=None):
    c = 0
    for _, d in _iter_detections(_load_events(), since_minutes):
        if label is None or d.get("label", "").lower() == str(label).lower():
            c += 1
    return {"label": label, "since_minutes": since_minutes, "count": c}

def most_common(since_minutes=None):
    counter = Counter()
    for _, d in _iter_detections(_load_events(), since_minutes):
        counter[d.get("label", "?")] += 1
    return {"since_minutes": since_minutes, "top": counter.most_common(3)}

def last_seen(label):
    if not label:                       # model passed null/empty -> "last detection of anything"
        return recent(1)
    latest = None
    for ts, d in _iter_detections(_load_events(), None):
        if d.get("label", "").lower() == str(label).lower():
            if latest is None or ts > latest:
                latest = ts
    return {"label": label, "last_seen": latest}

def labels_available():
    labels = {d.get("label", "?") for _, d in _iter_detections(_load_events(), None)}
    return {"labels": sorted(labels)}

def stats(op="avg", label=None, since_minutes=None):
    """Numeric stats over detection confidence. op: avg | min | max.
    One function covers 'average confidence', 'most confident detection',
    'lowest confidence person in the last hour', etc."""
    vals = []
    for _, d in _iter_detections(_load_events(), since_minutes):
        if label and d.get("label", "").lower() != str(label).lower():
            continue
        v = d.get("confidence")
        if v is not None:
            vals.append(float(v))
    if not vals:
        return {"op": op, "label": label, "since_minutes": since_minutes, "value": None, "n": 0}
    value = {"avg": round(sum(vals) / len(vals), 3),
             "min": min(vals),
             "max": max(vals)}.get(op, round(sum(vals) / len(vals), 3))
    return {"op": op, "label": label, "since_minutes": since_minutes, "value": value, "n": len(vals)}

def busiest(since_minutes=None):
    """Which hour of the day has the most detections."""
    buckets = Counter(ts[11:13] for ts, _ in _iter_detections(_load_events(), since_minutes))
    top = buckets.most_common(3)
    return {"since_minutes": since_minutes, "busiest_hour": top[0][0] if top else None, "counts": top}

FUNCTIONS = {
    "recent": recent,
    "count": count,
    "most_common": most_common,
    "last_seen": last_seen,
    "labels_available": labels_available,
    "stats": stats,
    "busiest": busiest,
}


# ============ THE MODEL: route, then phrase ============

ROUTER_SYSTEM = """You convert a question about camera detection data into ONE function call.

Functions:
- recent(n): the n most recent detection events, regardless of label. Use for
  "last detection", "latest", "what did you just see", "most recent". For "the last
  detection" use n=1.
- count(label, since_minutes): how many times a label was detected. label=null means everything.
- most_common(since_minutes): which labels are detected most often.
- last_seen(label): when a SPECIFIC named label was last detected. Only when the question
  names a specific object (e.g. "when did you last see a person"). Never call with label=null --
  if no specific label is named, use recent(1) instead.
- labels_available(): which labels have ever been detected ("what can you detect").
- stats(op, label, since_minutes): confidence statistics. op is "avg", "min", or "max".
  Use for "average/highest/lowest confidence", optionally for a specific label.
- busiest(since_minutes): which hour of the day has the most detections. Use for
  "busiest time", "when do you see the most", "peak hour".

Rules:
- Reply with ONLY JSON: {"function": "<name>", "args": {...}}
- Time to minutes: "last hour"->60, "today"->1440, "last 10 minutes"->10. No time mentioned -> null.
- If the question cannot be answered by these functions, reply {"function": "unknown", "args": {}}.
"""

def _ollama(messages, force_json=False, temperature=0.0):
    body = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }
    if force_json:
        body["format"] = "json"   # Ollama constrains output to valid JSON
    r = requests.post(OLLAMA_URL, json=body, timeout=120)
    r.raise_for_status()
    return r.json()["message"]["content"]

def route(question):
    content = _ollama(
        [{"role": "system", "content": ROUTER_SYSTEM},
         {"role": "user", "content": question}],
        force_json=True,
    )
    print("[route]", content)   # debug: remove once you're happy with routing
    try:
        return json.loads(content)
    except Exception:
        return {"function": "unknown", "args": {}}

def execute(call):
    name = call.get("function")
    args = call.get("args", {}) or {}
    if name not in FUNCTIONS:
        return None
    try:
        return FUNCTIONS[name](**args)
    except TypeError:
        return {"error": "bad_args", "call": call}

PHRASE_SYSTEM = """Answer the user's question in ONE short, plain-English sentence,
using ONLY the data provided. Never invent numbers. If the data is empty or null,
say that nothing matching was found."""

def phrase(question, result):
    if not USE_LLM_PHRASING:
        return str(result)
    return _ollama(
        [{"role": "system", "content": PHRASE_SYSTEM},
         {"role": "user", "content": f"Question: {question}\nData: {json.dumps(result)}"}],
        temperature=0.2,
    ).strip()

def answer(question):
    call = route(question)
    if call.get("function") == "unknown":
        return ("I can only answer questions about detections: counts, recent events, "
                "the most common label, confidence stats, busiest times, when a specific "
                "thing was last seen, or which labels I track.")
    result = execute(call)
    if result is None:
        return "I couldn't map that to something I can look up."
    return phrase(question, result)


# ============ optional: log each Q&A to DynamoDB via MQTT ============

_session_id = str(uuid.uuid4())[:8]
_chat_pub = None
if LOG_CHAT_TO_AWS:
    try:
        from aws_publisher import AWSPublisher
        _chat_pub = AWSPublisher(
            endpoint=AWS_ENDPOINT,
            ca_path=CA_PATH,
            cert_path=CERT_PATH,
            key_path=KEY_PATH,
            topic=CHAT_TOPIC,
            client_id="assistant-dev",   # distinct from camera.py's client_id
        )
    except Exception as e:
        print(f"(chat logging disabled: {e})")

def log_chat(question, reply):
    if _chat_pub is None:
        return
    event = {
        "session_id": _session_id,
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f"),
        "question": question,
        "answer": reply,
    }
    try:
        _chat_pub.publish(event, add_sensor_id=False)   # keep chat_logs rows clean
    except Exception as e:
        print(f"(chat log failed: {e})")


# ============ simple REPL ============

if __name__ == "__main__":
    print(f"Assistant ready (model={MODEL}, session={_session_id}).")
    print("Ask about detections. Ctrl-C to quit.\n")
    while True:
        try:
            q = input("> ").strip()
            if not q:
                continue
            reply = answer(q)
            print(reply + "\n")
            log_chat(q, reply)
        except (KeyboardInterrupt, EOFError):
            print("\nbye")
            if _chat_pub is not None:
                _chat_pub.close()
            break
