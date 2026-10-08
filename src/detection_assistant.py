"""
Natural-language assistant over your camera detection data.

Design (the important part):
  - The functions below are the ONLY code that touches the data. They are
    deterministic and testable. This is your "capabilities" set.
  - The SLM (a premade model, via Ollama) does TWO things and nothing else:
      1) route: map the user's phrasing -> one function + args
      2) phrase: turn the function's result into a plain-English sentence
  - The model never counts or invents numbers. Your code computes; the model
    only translates. That's why a handful of functions cover infinite phrasings.

Architecture (docs/adr/0003-detection-assistant.md):
  - Runs as a SEPARATE process from main.py and reads the same SQLite log
    (logs/events.db), opened read-only. WAL mode lets it read while main.py writes.
  - Counts are VISITS (enter events), not per-frame detections.

Run:
  1) ollama serve            (usually already running as a service)
  2) ollama pull <MODEL>     (the MODEL constant below)
  3) python src/detection_assistant.py
"""

import json
import os
import sqlite3
import uuid
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

# ---------------- config ----------------
#Paths anchored to this file, like main.py, so it runs from any directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH      = PROJECT_ROOT / "logs" / "events.db"   # same file main.py writes to

OLLAMA_URL   = "http://localhost:11434/api/chat"
MODEL        = "gemma2:2b"      # try "qwen2.5:3b" if routing is flaky
USE_LLM_PHRASING = True         # False = skip 2nd model call, faster but less natural

# chat logging to DynamoDB via MQTT (reuses your IoT cert -- no new credentials).
# A second IoT rule routes topic 'chat/logs' -> chat_logs table (docs/aws-setup.md).
# Off until that table, rule, and policy exist. If True, missing config is an
# error at startup (same fail-fast rule as main.py).
LOG_CHAT_TO_AWS = False
AWS_CLIENT_ID   = "assistant-dev"   # must differ from main.py's, or they kick each other off
CERT_NAME       = "detector-01"     # same device cert as main.py
CERTS_DIR       = PROJECT_ROOT / "certs"
CHAT_TOPIC      = "chat/logs"

#AWS_ENDPOINT lives in .env (kept out of git)
load_dotenv(PROJECT_ROOT / ".env")


# ============ DATA LAYER: the only things that read the DB ============

def _query(sql, params=()):
    """Open fresh each call to see the latest data. mode=ro: SQLite itself
    refuses writes, so this process can never modify the log."""
    if not DB_PATH.exists():
        raise FileNotFoundError(f"No detection log yet at {DB_PATH}. Run main.py first.")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()

def _utc_cutoff(since_minutes):
    """ISO UTC string N minutes ago, in the SAME format main.py stores
    (object_detection.utc_timestamp), so string comparison == time comparison."""
    t = datetime.now(timezone.utc) - timedelta(minutes=float(since_minutes))
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")

def _local(ts):
    """UTC ISO from the DB -> local wall-clock text. Done in code so the model
    never does time-zone math (it would read UTC out loud as local time)."""
    if ts is None:
        return None
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return t.astimezone().strftime("%Y-%m-%d %I:%M:%S %p")

def _since_clause(since_minutes, column):
    if since_minutes is None:
        return "", ()
    return f" AND {column} >= ?", (_utc_cutoff(since_minutes),)

def _normalize_label(label):
    """Map the model's phrasing ("People", "cars") onto YOLO's labels
    ("person", "car"). None stays None (= everything)."""
    if label is None:
        return None
    label = str(label).strip().lower()
    known = {row["class_name"] for row in _query("SELECT DISTINCT class_name FROM events")}
    irregular = {"people": "person", "persons": "person", "men": "person", "women": "person"}
    for candidate in (label, irregular.get(label), label[:-1] if label.endswith("s") else None):
        if candidate in known:
            return candidate
    return label   # unknown label: queries simply return 0 / nothing

def _latest_run_id():
    rows = _query("SELECT run_id FROM events ORDER BY id DESC LIMIT 1")
    return rows[0]["run_id"] if rows else None

# ---- the capabilities ----

def recent(n=5):
    visits = _query(
        "SELECT class_name, entered_at, exited_at FROM tracks "
        "ORDER BY entered_at DESC LIMIT ?", (int(n),))
    return {"visits": [
        {"label": v["class_name"],
         "arrived": _local(v["entered_at"]),
         "left": _local(v["exited_at"]) if v["exited_at"] else "still present"}
        for v in visits]}

def count(label=None, since_minutes=None):
    label = _normalize_label(label)
    label_sql, label_params = ("", ()) if label is None else (" AND class_name = ?", (label,))
    since_sql, since_params = _since_clause(since_minutes, "ts")
    rows = _query(
        "SELECT COUNT(*) AS n FROM events WHERE event_type = 'enter'" + label_sql + since_sql,
        label_params + since_params)
    return {"label": label, "since_minutes": since_minutes, "visits": rows[0]["n"]}

def most_common(since_minutes=None):
    since_sql, since_params = _since_clause(since_minutes, "ts")
    rows = _query(
        "SELECT class_name, COUNT(*) AS n FROM events WHERE event_type = 'enter'" + since_sql +
        " GROUP BY class_name ORDER BY n DESC LIMIT 3", since_params)
    return {"since_minutes": since_minutes, "top": [[r["class_name"], r["n"]] for r in rows]}

def last_seen(label):
    if not label:                       # model passed null/empty -> treat as "last detection of anything"
        return recent(1)
    label = _normalize_label(label)
    rows = _query(
        "SELECT entered_at, exited_at FROM tracks WHERE class_name = ? "
        "ORDER BY entered_at DESC LIMIT 1", (label,))
    if not rows:
        return {"label": label, "last_seen": None}
    #exited_at is the last sighting (see ADR 0001); NULL = still in view
    if rows[0]["exited_at"] is None:
        return {"label": label, "last_seen": "right now (still in view)"}
    return {"label": label, "last_seen": _local(rows[0]["exited_at"])}

def labels_available():
    rows = _query("SELECT DISTINCT class_name FROM events ORDER BY class_name")
    return {"labels": [r["class_name"] for r in rows]}

def present_now():
    """Open visits in the latest run. Caveat: if main.py crashed (not 'q' or
    Ctrl+C), its last visits never got exit events and show here until the
    next run starts."""
    run_id = _latest_run_id()
    if run_id is None:
        return {"present": []}
    rows = _query(
        "SELECT class_name, entered_at FROM tracks "
        "WHERE run_id = ? AND exited_at IS NULL ORDER BY entered_at", (run_id,))
    return {"present": [{"label": r["class_name"], "since": _local(r["entered_at"])} for r in rows]}

FUNCTIONS = {
    "recent": recent,
    "count": count,
    "most_common": most_common,
    "last_seen": last_seen,
    "labels_available": labels_available,
    "present_now": present_now,
}


# ============ THE MODEL: route, then phrase ============

ROUTER_SYSTEM = """You convert a question about camera detection data into ONE function call.
Each time an object enters the camera's view counts as one "visit".

Functions:
- present_now(): what is in view RIGHT NOW. Use for "is anyone there", "what do you see now",
  "who's here".
- recent(n): the n most recent visits, regardless of label. Use this for
  "last detection", "latest", "what did you just see", "most recent". For "the last
  detection" use n=1.
- count(label, since_minutes): how many visits by a label. label=null means everything.
- most_common(since_minutes): which labels visit most often
- last_seen(label): when a SPECIFIC named label was last seen. Only use this when the
  question names a specific object (e.g. "when did you last see a person"). Never call it
  with label=null -- if no specific label is named, use recent(1) instead.
- labels_available(): which labels have ever been detected

Rules:
- Reply with ONLY JSON: {"function": "<name>", "args": {...}}
- Labels are singular and lowercase, e.g. "person", "car", "dog".
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

def route(question, verbose=True):
    content = _ollama(
        [{"role": "system", "content": ROUTER_SYSTEM},
         {"role": "user", "content": question}],
        force_json=True,
    )
    if verbose:
        print("[route]", content)   # debug: shows the model's raw decision
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
    except (TypeError, ValueError):
        # model passed wrong/extra args -- be lenient rather than crash
        return {"error": "bad_args", "call": call}

PHRASE_SYSTEM = """Answer the user's question in ONE short, plain-English sentence,
using ONLY the data provided. Never invent numbers. Times are already in local time.
If the data is empty or null, say that nothing matching was found."""

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
        return ("I can only answer questions about detections: what's in view now, counts, "
                "recent visits, the most common label, when something was last seen, "
                "or which labels I track.")
    result = execute(call)
    if result is None:
        return "I couldn't map that to something I can look up."
    return phrase(question, result)


# ============ optional: log each Q&A to DynamoDB ============

_session_id = str(uuid.uuid4())[:8]

def _make_chat_publisher():
    if not LOG_CHAT_TO_AWS:
        return None
    endpoint = os.getenv("AWS_ENDPOINT")
    if not endpoint:
        raise RuntimeError("LOG_CHAT_TO_AWS is True but AWS_ENDPOINT is not set in .env")
    from aws_publisher import AWSPublisher
    #Missing cert files raise FileNotFoundError here (fail fast on config)
    return AWSPublisher(
        endpoint=endpoint,
        ca_path=str(CERTS_DIR / "AmazonRootCA1.pem"),
        cert_path=str(CERTS_DIR / f"{CERT_NAME}.cert.pem"),
        key_path=str(CERTS_DIR / f"{CERT_NAME}.private.key"),
        topic=CHAT_TOPIC,
        client_id=AWS_CLIENT_ID,
    )

def log_chat(publisher, question, reply):
    #Direct publish, NOT store-and-forward: a chat log sent while offline is
    #lost on exit. Acceptable for chat history (ADR 0003).
    if publisher is None:
        return
    event = {
        "session_id": _session_id,
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f"),
        "question": question,
        "answer": reply,
    }
    publisher.publish(event)


# ============ simple REPL ============

if __name__ == "__main__":
    chat_pub = _make_chat_publisher()
    print(f"Assistant ready (model={MODEL}, session={_session_id}).")
    print("Ask about detections. Ctrl-C to quit.\n")
    while True:
        try:
            q = input("> ").strip()
            if not q:
                continue
            reply = answer(q)
            print(reply + "\n")
            log_chat(chat_pub, q, reply)
        except requests.RequestException as e:
            print(f"(couldn't reach Ollama at {OLLAMA_URL}: is `ollama serve` running "
                  f"and `{MODEL}` pulled? {e})\n")
        except (FileNotFoundError, sqlite3.Error) as e:
            print(f"(detection log unavailable: {e})\n")
        except (KeyboardInterrupt, EOFError):
            print("\nbye")
            if chat_pub is not None:
                chat_pub.close()
            break
