"""
Routing eval for the detection assistant (docs/adr/0004-structured-model-output.md).

Runs each question through route() only (no phrasing, no DB answer) and checks
the call it produces against the expected one. Use the score to compare prompt
changes or models, instead of judging by feel.

Run (Ollama must be running):
  python src/eval_assistant.py                       # tuning set, MODEL from detection_assistant
  python src/eval_assistant.py llama3.2:1b           # try another model
  python src/eval_assistant.py --held-out            # held-out set (see below)

Two sets:
  CASES     the tuning set. Prompt changes were made while looking at these
            failures, so its score is optimistic.
  HELD_OUT  never used for tuning: the honest estimate. Run it to CHECK a change,
            never to MAKE one. Once you tune against a held-out failure, it's no
            longer held out; write new held-out questions instead.

Keep both sets OUT of the router prompt's examples: if the prompt contains the
test questions, the score measures memorization, not understanding.
"""

import sys
import time

import detection_assistant as da

ANY = object()   #expected arg value: any value is fine, as long as it's valid

#(question, expected function, expected args). Args not listed aren't checked;
#None means the arg must be null/absent (e.g. "no time mentioned" -> all time).
CASES = [
    ("is anyone there?",                              "present_now",      {}),
    ("who's here right now?",                         "present_now",      {}),
    ("what do you see at this moment?",               "present_now",      {}),
    ("what did you just see?",                        "recent",           {"n": 1}),
    ("what was the last thing you saw?",              "recent",           {"n": 1}),
    ("show me the last 3 detections",                 "recent",           {"n": 3}),
    ("what has been detected lately?",                "recent",           {"n": ANY}),
    ("how many people came by in the last hour?",     "count",            {"label": "person", "since_minutes": 60}),
    ("how many cars today?",                          "count",            {"label": "car", "since_minutes": 1440}),
    ("how many visits in the last 10 minutes?",       "count",            {"label": None, "since_minutes": 10}),
    ("how many people have you seen?",                "count",            {"label": "person", "since_minutes": None}),
    ("how many dogs were there?",                     "count",            {"label": "dog", "since_minutes": None}),
    ("what do you see most often?",                   "most_common",      {"since_minutes": None}),
    ("what was the most common thing in the last hour?", "most_common",   {"since_minutes": 60}),
    ("when did you last see a cell phone?",           "last_seen",        {"label": "cell phone"}),
    ("when was the last time a dog showed up?",       "last_seen",        {"label": "dog"}),
    ("what kinds of things can you detect?",          "labels_available", {}),
    ("which objects have you ever seen?",             "labels_available", {}),
    ("what's the weather like?",                      "unknown",          {}),
    ("tell me a joke",                                "unknown",          {}),
]

#Never used for tuning (first run 2026-10-08, gemma2:2b: 7/8)
HELD_OUT = [
    ("is the room empty?",                              "present_now",      {}),
    ("give me the five latest sightings",               "recent",           {"n": 5}),
    ("how many bicycles passed in the last half hour?", "count",            {"label": "bicycle", "since_minutes": 30}),
    ("total number of visits so far?",                  "count",            {"label": None, "since_minutes": None}),
    ("what's the top object this past day?",            "most_common",      {"since_minutes": 1440}),
    ("when was a laptop last around?",                  "last_seen",        {"label": "laptop"}),
    ("list every label you've recorded",                "labels_available", {}),
    ("can you turn on the lights?",                     "unknown",          {}),
]


def _same_label(got, want):
    #Compare the way the code will use it: case-insensitive, simple plurals
    if got is None or want is None:
        return got is None and want is None
    got = str(got).strip().lower()
    return got == want or got.rstrip("s") == want or (want == "person" and got == "people")


def check(call, function, args):
    """Returns None if the call matches, else a short reason."""
    if not isinstance(call, dict) or call.get("function") != function:
        return f"function {call.get('function') if isinstance(call, dict) else call!r}"
    got_args = call.get("args") or {}
    for key, want in args.items():
        got = got_args.get(key)
        if want is ANY:
            if got is None:
                return f"{key} missing"
        elif key == "label":
            if not _same_label(got, want):
                return f"label {got!r}"
        elif got != want:
            return f"{key} {got!r}"
    return None


def main():
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    models = [a for a in sys.argv[1:] if not a.startswith("--")]
    if models:
        da.MODEL = models[0]
    cases, name = (HELD_OUT, "held-out") if "--held-out" in flags else (CASES, "tuning")
    print(f"model={da.MODEL}  set={name}  cases={len(cases)}\n")

    passed, start = 0, time.time()
    for question, function, args in cases:
        call = da.route(question, verbose=False)
        reason = check(call, function, args)
        passed += reason is None
        mark = "PASS" if reason is None else f"FAIL ({reason})"
        print(f"{mark:<28} {question}\n{'':<28} -> {call}")

    print(f"\nscore: {passed}/{len(cases)}  ({time.time() - start:.0f}s)")


if __name__ == "__main__":
    main()
