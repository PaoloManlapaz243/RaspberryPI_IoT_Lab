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

def _time_window(since_minutes):
    """since_minutes -> plain words for the phrasing model. Results never pass it
    a raw null: to our code null means "all time", but the model reads it as
    "missing" and says nothing was found."""
    if since_minutes is None:
        return "all time"
    m = int(since_minutes)
    if m % 60 == 0:
        hours = m // 60
        return "the last hour" if hours == 1 else f"the last {hours} hours"
    return "the last minute" if m == 1 else f"the last {m} minutes"

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
    return {"label": label or "anything", "time_window": _time_window(since_minutes),
            "visits": rows[0]["n"]}

def most_common(since_minutes=None):
    since_sql, since_params = _since_clause(since_minutes, "ts")
    rows = _query(
        "SELECT class_name, COUNT(*) AS n FROM events WHERE event_type = 'enter'" + since_sql +
        " GROUP BY class_name ORDER BY n DESC LIMIT 3", since_params)
    return {"time_window": _time_window(since_minutes),
            "top": [[r["class_name"], r["n"]] for r in rows]}

def last_seen(label):
    #label is required: validate_call() rejects a missing one (use recent(1) instead)
    label = _normalize_label(label)
    rows = _query(
        "SELECT entered_at, exited_at FROM tracks WHERE class_name = ? "
        "ORDER BY entered_at DESC LIMIT 1", (label,))
    if not rows:
        return {"label": label, "last_seen": "never"}
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

Functions (name: args):
- present_now: no args. What is in view RIGHT NOW ("is anyone there", "who's here").
- recent: n. The n most recent visits of anything. "what did you just see" or
  "the last thing" -> n=1.
- count: label, since_minutes. HOW MANY visits. label=null counts every label.
- most_common: since_minutes. Which labels visit MOST OFTEN.
- last_seen: label. WHEN a specific named object was last seen.
- labels_available: no args. Which label NAMES have ever been seen (a list, not counts).
- unknown: no args. Anything the functions above can't answer.

Rules:
- Labels are singular and lowercase: "person", "car", "dog", "cell phone".
- since_minutes is a time window: "last hour"->60, "today"->1440, "past 30 minutes"->30.
  If the question gives NO time window, since_minutes is null (all time). Never use 0.
- "How many" questions use count, even when they mention visits.

Examples:
Q: how many trucks have shown up in the past 30 minutes?
A: {"function": "count", "args": {"label": "truck", "since_minutes": 30}}
Q: has anything at all come by in the last 2 hours?
A: {"function": "count", "args": {"label": null, "since_minutes": 120}}
Q: what type of object shows up the most overall?
A: {"function": "most_common", "args": {"since_minutes": null}}
Q: anything in front of the camera?
A: {"function": "present_now", "args": {}}
"""

#Which args each function accepts. The single source for validation; the
#schema's function names come from FUNCTIONS, so neither can drift from the code.
ALLOWED_ARGS = {
    "present_now": set(),
    "recent": {"n"},
    "count": {"label", "since_minutes"},
    "most_common": {"since_minutes"},
    "last_seen": {"label"},
    "labels_available": set(),
    "unknown": set(),
}
#Args that must be present (non-null). Missing -> rejected, so the model retries
REQUIRED_ARGS = {"last_seen": {"label"}}

#Fail at startup if a function is added to FUNCTIONS without its args here
assert set(ALLOWED_ARGS) == set(FUNCTIONS) | {"unknown"}, "ALLOWED_ARGS out of sync with FUNCTIONS"

#Ollama enforces this WHILE the model generates (constrained decoding): tokens
#that would break it are never produced, so e.g. "present_now()" is impossible.
#It guarantees shape and types only; validate_call() checks meaning.
ROUTER_SCHEMA = {
    "type": "object",
    "required": ["function", "args"],
    "properties": {
        "function": {"type": "string", "enum": list(FUNCTIONS) + ["unknown"]},
        "args": {
            "type": "object",
            "properties": {
                "label":         {"type": ["string", "null"]},
                "since_minutes": {"type": ["integer", "null"]},
                "n":             {"type": "integer"},
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}

class InvalidCall(ValueError):
    pass

def validate_call(call):
    """Check a routed call against ALLOWED_ARGS and value rules. Returns a clean
    {"function", "args"} dict, or raises InvalidCall saying what's wrong.
    Rejects rather than guesses: a wrong guess silently runs the wrong query."""
    if not isinstance(call, dict):
        raise InvalidCall("reply must be a JSON object")
    name = call.get("function")
    if name not in ALLOWED_ARGS:
        raise InvalidCall(f"unknown function {name!r}")

    args = {}
    for key, value in (call.get("args") or {}).items():
        if value is None:
            continue                     #null = not given
        if key not in ALLOWED_ARGS[name]:
            raise InvalidCall(f"{name} does not take {key}")
        args[key] = value

    missing = REQUIRED_ARGS.get(name, set()) - set(args)
    if missing:
        hint = " (for the last thing of any kind, use recent with n=1)" if name == "last_seen" else ""
        raise InvalidCall(f"{name} requires {', '.join(sorted(missing))}{hint}")

    if "since_minutes" in args:
        m = args["since_minutes"]
        if isinstance(m, bool) or not isinstance(m, int) or m < 1:
            raise InvalidCall("since_minutes must be a whole number >= 1, or null for all time")
    if "n" in args:
        n = args["n"]
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 50:
            raise InvalidCall("n must be a whole number from 1 to 50")
    if "label" in args and not (isinstance(args["label"], str) and args["label"].strip()):
        raise InvalidCall("label must be a non-empty string, or null")

    return {"function": name, "args": args}

def _ollama(messages, schema=None, temperature=0.0):
    body = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }
    if schema is not None:
        body["format"] = schema   # constrain output to this JSON schema
    r = requests.post(OLLAMA_URL, json=body, timeout=120)
    r.raise_for_status()
    return r.json()["message"]["content"]

def route(question, verbose=True):
    """Question -> validated call. One retry: the model sees its invalid reply
    and the reason. Returns {"function": "invalid", ...} if both attempts fail."""
    messages = [{"role": "system", "content": ROUTER_SYSTEM},
                {"role": "user", "content": question}]
    error = None
    for attempt in range(2):
        content = _ollama(messages, schema=ROUTER_SCHEMA)
        if verbose:
            print("[route]", content)   # debug: shows the model's raw decision
        try:
            return validate_call(json.loads(content))
        except ValueError as e:         #bad JSON or InvalidCall
            error = str(e)
            if verbose:
                print("[route] invalid:", error)
            messages += [{"role": "assistant", "content": content},
                         {"role": "user", "content": f"That call is invalid: {error}. "
                                                     "Reply with the corrected JSON only."}]
    return {"function": "invalid", "args": {}, "error": error}

def execute(call):
    #Only ever given validated calls (see route), so args are known-good
    return FUNCTIONS[call["function"]](**call["args"])

PHRASE_SYSTEM = """Answer the user's question in ONE short, plain-English sentence,
using ONLY the data provided. Never invent numbers. Times are already in local time.
Use the time_window exactly as given (e.g. "the last hour", "all time").
Only if a count is 0, a list is empty, or last_seen is "never", say that nothing
matching was found."""

def _assert_no_nulls(value, path="result"):
    #Results go to the model as JSON; a null there gets misread as "nothing
    #found". Fail loudly in development instead of answering wrongly.
    if value is None:
        raise AssertionError(f"{path} is null; give the model plain words instead")
    if isinstance(value, dict):
        for k, v in value.items():
            _assert_no_nulls(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _assert_no_nulls(v, f"{path}[{i}]")

def phrase(question, result):
    _assert_no_nulls(result)
    if not USE_LLM_PHRASING:
        return str(result)
    return _ollama(
        [{"role": "system", "content": PHRASE_SYSTEM},
         {"role": "user", "content": f"Question: {question}\nData: {json.dumps(result)}"}],
        temperature=0.2,
    ).strip()

def answer(question):
    call = route(question)
    if call["function"] == "unknown":
        return ("I can only answer questions about detections: what's in view now, counts, "
                "recent visits, the most common label, when something was last seen, "
                "or which labels I track.")
    if call["function"] == "invalid":
        return ("I wasn't sure how to look that up. Try rephrasing, e.g. with an object "
                "name or a time range like 'in the last hour'.")
    return phrase(question, execute(call))


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
