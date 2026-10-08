"""
Routing eval for the detection assistant (docs/adr/0004-structured-model-output.md,
docs/adr/0005-routing-evaluation.md).

Runs each question through route() only (no phrasing, no DB answer) and checks
the call it produces against the expected one. Use the score to compare prompt
changes or models, instead of judging by feel.

Run (Ollama must be running):
  python src/eval_assistant.py                       # tuning set, MODEL from detection_assistant
  python src/eval_assistant.py llama3.2:1b           # try another model
  python src/eval_assistant.py --held-out            # held-out set (see below)
  python src/eval_assistant.py --verbose             # print passing cases too
  python src/eval_assistant.py --no-guard            # measure without the code guard (ADR 0005)

Two sets:
  CASES     the tuning set. Prompt changes are made while looking at these
            failures, so its score is optimistic.
  HELD_OUT  never used for tuning: the honest estimate. Run it to CHECK a change,
            never to MAKE one. Once you tune against a held-out failure, it's no
            longer held out; write new held-out questions instead.

Keep both sets OUT of the router prompt's examples: if the prompt contains the
test questions, the score measures memorization, not understanding.

Gate for the off-topic chat feature (ADR 0005): held-out accuracy >= 90% AND
zero detection questions routed to chat.
"""

import sys
import time
from collections import Counter

import detection_assistant as da

ANY = object()   #expected arg value: any value is fine, as long as it's valid

class ONE_OF:
    #expected arg value: any of these is correct (genuinely ambiguous questions)
    def __init__(self, *values):
        self.values = values

#Intents that send the question to chat (no data). A detection question routed
#here is the dangerous failure: the answer would be invented.
CHAT_INTENTS = {"unknown", "off_topic"}

#Outcomes that decline without data and without chat
SAFE_DECLINES = {"unclear", "invalid"}

GATE_ACCURACY = 0.90

#(question, expected function, expected args). Args not listed aren't checked;
#None means the arg must be null/absent (e.g. "no time mentioned" -> all time).
#"unclear" = about detections, but no function can answer it (unsupported time
#ranges, several labels at once, durations, attributes). Must never go to chat.
CASES = [
    # --- present_now
    ("is anyone there?",                              "present_now",      {}),
    ("who's here right now?",                         "present_now",      {}),
    ("what do you see at this moment?",               "present_now",      {}),
    ("anybody in the room right now?",                "present_now",      {}),
    ("is the area clear?",                            "present_now",      {}),
    ("what's currently on camera?",                   "present_now",      {}),
    # --- recent
    ("what did you just see?",                        "recent",           {"n": 1}),
    ("what was the last thing you saw?",              "recent",           {"n": 1}),
    ("show me the last 3 detections",                 "recent",           {"n": 3}),
    ("what has been detected lately?",                "recent",           {"n": ANY}),
    ("what were the last two things that showed up?", "recent",           {"n": 2}),
    ("anything new lately?",                          "recent",           {"n": ANY}),
    ("most recent sighting?",                         "recent",           {"n": 1}),
    # --- count
    ("how many people came by in the last hour?",     "count",            {"label": "person", "since_minutes": 60}),
    ("how many cars today?",                          "count",            {"label": "car", "since_minutes": 1440}),
    ("how many visits in the last 10 minutes?",       "count",            {"label": None, "since_minutes": 10}),
    ("how many people have you seen?",                "count",            {"label": "person", "since_minutes": None}),
    ("how many dogs were there?",                     "count",            {"label": "dog", "since_minutes": None}),
    ("how many humans came by in the past 15 minutes?", "count",          {"label": "person", "since_minutes": 15}),
    ("number of cats this hour?",                     "count",            {"label": "cat", "since_minutes": 60}),
    ("how mny ppl came by today",                     "count",            {"label": "person", "since_minutes": 1440}),
    ("count every visit in the last 2 hours",         "count",            {"label": None, "since_minutes": 120}),
    ("hey uh how many trucks have there been",        "count",            {"label": "truck", "since_minutes": None}),
    ("how many phones were seen?",                    "count",            {"label": "cell phone", "since_minutes": None}),
    ("has nobody come by in the last hour?",          "count",            {"label": ONE_OF(None, "person"), "since_minutes": 60}),
    # --- most_common
    ("what do you see most often?",                   "most_common",      {"since_minutes": None}),
    ("what was the most common thing in the last hour?", "most_common",   {"since_minutes": 60}),
    ("which thing appears most frequently?",          "most_common",      {"since_minutes": None}),
    ("top visitor in the past 3 hours?",              "most_common",      {"since_minutes": 180}),
    # --- last_seen
    ("when did you last see a cell phone?",           "last_seen",        {"label": "cell phone"}),
    ("when was the last time a dog showed up?",       "last_seen",        {"label": "dog"}),
    ("when did a bike last pass?",                    "last_seen",        {"label": "bicycle"}),
    ("last time you saw a human?",                    "last_seen",        {"label": "person"}),
    ("when was the cat last here",                    "last_seen",        {"label": "cat"}),
    # --- labels_available
    ("what kinds of things can you detect?",          "labels_available", {}),
    ("which objects have you ever seen?",             "labels_available", {}),
    ("what labels exist in your log?",                "labels_available", {}),
    ("give me all the object types you've logged",    "labels_available", {}),
    # --- unclear: detection questions no function can answer
    ("how many people came by since 9am?",            "unclear",          {}),
    ("how many dogs yesterday?",                      "unclear",          {}),
    ("how many vehicles passed?",                     "unclear",          {}),
    ("how many people and dogs came by?",             "unclear",          {}),
    ("how long did the last person stay?",            "unclear",          {}),
    ("was anyone here at 3pm?",                       "unclear",          {}),
    ("is anyone wearing a hat?",                      "unclear",          {}),
    # --- off-topic
    ("what's the weather like?",                      "unknown",          {}),
    ("tell me a joke",                                "unknown",          {}),
    ("what's 2+2?",                                   "unknown",          {}),
    ("who won the world cup?",                        "unknown",          {}),
    ("hello!",                                        "unknown",          {}),
]

#Never used for tuning. The first 8 were first run 2026-10-08 (gemma2:2b: 7/8)
#and only ever checked; the rest were written before any tuning on this set.
HELD_OUT = [
    ("is the room empty?",                              "present_now",      {}),
    ("give me the five latest sightings",               "recent",           {"n": 5}),
    ("how many bicycles passed in the last half hour?", "count",            {"label": "bicycle", "since_minutes": 30}),
    ("total number of visits so far?",                  "count",            {"label": None, "since_minutes": None}),
    ("what's the top object this past day?",            "most_common",      {"since_minutes": 1440}),
    ("when was a laptop last around?",                  "last_seen",        {"label": "laptop"}),
    ("list every label you've recorded",                "labels_available", {}),
    ("can you turn on the lights?",                     "unknown",          {}),
    ("anyone around?",                                  "present_now",      {}),
    ("is it empty in there?",                           "present_now",      {}),
    ("what objects are visible right now?",             "present_now",      {}),
    ("what came through most recently?",                "recent",           {"n": 1}),
    ("list the latest 4 visits",                        "recent",           {"n": 4}),
    ("how many folks stopped by in the last 45 minutes?", "count",          {"label": "person", "since_minutes": 45}),
    ("visits by dogs in the past day?",                 "count",            {"label": "dog", "since_minutes": 1440}),
    ("how many total detections in the last hour?",     "count",            {"label": None, "since_minutes": 60}),
    ("how many motorcycles have you counted?",          "count",            {"label": "motorcycle", "since_minutes": None}),
    ("um how many ppl today",                           "count",            {"label": "person", "since_minutes": 1440}),
    ("what's the most frequent object in the last 30 minutes?", "most_common", {"since_minutes": 30}),
    ("when did someone last walk by?",                  "last_seen",        {"label": "person"}),
    ("last sighting of a car?",                         "last_seen",        {"label": "car"}),
    ("what categories have you detected so far?",       "labels_available", {}),
    ("how many people came in this morning?",           "unclear",          {}),
    ("were there more cars or trucks?",                 "unclear",          {}),
    ("how many people came between noon and 2pm?",      "unclear",          {}),
    ("what color was the car?",                         "unclear",          {}),
    ("how many animals came by?",                       "unclear",          {}),
    ("what time is it?",                                "unknown",          {}),
    ("recommend a movie",                               "unknown",          {}),
    ("good morning",                                    "unknown",          {}),
    #Added 2026-10-08 before any run, to compare label approaches (ADR 0006).
    #Synonyms the old hand-written table never listed:
    ("how many pedestrians walked past in the last hour?", "count",         {"label": "person", "since_minutes": 60}),
    ("when did you last see a lad?",                    "last_seen",        {"label": "person"}),
    ("how many automobiles today?",                     "count",            {"label": "car", "since_minutes": 1440}),
    ("how many pups showed up?",                        "count",            {"label": "dog", "since_minutes": None}),
    ("when was a pushbike last seen?",                  "last_seen",        {"label": "bicycle"}),
    ("how many smartphones have been spotted?",         "count",            {"label": "cell phone", "since_minutes": None}),
    ("when did a feline last appear?",                  "last_seen",        {"label": "cat"}),
    ("how many lorries passed in the last 2 hours?",    "count",            {"label": "truck", "since_minutes": 120}),
    #Categories (several detector labels): must decline, not pick one label
    ("how many electronics were seen?",                 "unclear",          {}),
    ("how much furniture has been detected?",           "unclear",          {}),
]


def _same_label(got, want):
    #Compare the way the code will use it: case-insensitive, simple plurals
    if got is None or want is None:
        return got is None and want is None
    got = str(got).strip().lower()
    return got == want or got.rstrip("s") == want or (want == "person" and got == "people")


def check(call, function, args):
    """Returns None if the call matches, else a short reason."""
    #For an unclear question, invalid is equally correct: both decline with
    #"rephrase", neither reads the DB nor goes to chat
    if function == "unclear" and isinstance(call, dict) and call.get("function") in SAFE_DECLINES:
        return None
    if not isinstance(call, dict) or call.get("function") != function:
        return f"function {call.get('function') if isinstance(call, dict) else call!r}"
    got_args = call.get("args") or {}
    for key, want in args.items():
        got = got_args.get(key)
        wants = want.values if isinstance(want, ONE_OF) else (want,)
        if want is ANY:
            if got is None:
                return f"{key} missing"
        elif key == "label":
            if not any(_same_label(got, w) for w in wants):
                return f"label {got!r}"
        elif got not in wants:
            return f"{key} {got!r}"
    return None


def _is_synonym_case(question, args):
    #The expected label isn't written in the question: the word had to be mapped
    #("pedestrians" -> person). Measures label grouping on its own.
    label = args.get("label")
    if not isinstance(label, str):
        return False
    q = question.lower()
    return label not in q and f"{label}s" not in q


def _instrument():
    #Wrap assistant internals to count, per question, model calls and validation
    #rejections (each rejection triggers a retry), without changing the assistant
    counts = {"calls": 0, "rejections": 0}
    real_ollama, real_validate = da._ollama, da.validate_call
    def ollama(*a, **k):
        counts["calls"] += 1
        return real_ollama(*a, **k)
    def validate(*a, **k):
        try:
            return real_validate(*a, **k)
        except ValueError:
            counts["rejections"] += 1
            raise
    da._ollama, da.validate_call = ollama, validate
    return counts


def main():
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    models = [a for a in sys.argv[1:] if not a.startswith("--")]
    if models:
        da.MODEL = models[0]
    verbose = "--verbose" in flags
    if "--no-guard" in flags:
        da.USE_GUARD = False
    held_out = "--held-out" in flags
    cases, name = (HELD_OUT, "held-out") if held_out else (CASES, "tuning")
    print(f"model={da.MODEL}  set={name}  cases={len(cases)}  "
          f"guard={da.USE_GUARD}\n")

    counts = _instrument()
    passed, retried, total_calls, to_chat = 0, 0, 0, []
    synonyms, declines = [0, 0], [0, 0]   #[passed, total]
    confusions, latencies = Counter(), []
    for question, function, args in cases:
        counts.update(calls=0, rejections=0)
        start = time.time()
        call = da.route(question, verbose=False)
        latencies.append(time.time() - start)
        retried += counts["rejections"] > 0
        total_calls += counts["calls"]

        reason = check(call, function, args)
        passed += reason is None
        if _is_synonym_case(question, args):
            synonyms[0] += reason is None; synonyms[1] += 1
        if function == "unclear":
            declines[0] += reason is None; declines[1] += 1
        routed = call.get("function")
        if routed != function and not (function == "unclear" and routed in SAFE_DECLINES):
            confusions[(function, routed)] += 1
        if function not in CHAT_INTENTS and routed in CHAT_INTENTS:
            to_chat.append(question)

        if reason is not None or verbose:
            mark = "PASS" if reason is None else f"FAIL ({reason})"
            print(f"{mark:<28} {question}\n{'':<28} -> {call}")

    accuracy = passed / len(cases)
    print(f"\nscore: {passed}/{len(cases)} ({accuracy:.0%})")
    print(f"synonym cases: {synonyms[0]}/{synonyms[1]}  (label had to be mapped from another word)")
    print(f"unclear declines: {declines[0]}/{declines[1]}  (must decline: times, categories, several labels...)")

    print("\nconfusions (expected -> routed):")
    for (expected, routed), n in confusions.most_common():
        print(f"  {expected:>16} -> {routed:<16} x{n}")
    if not confusions:
        print("  none")

    print(f"\nmisroutes to chat: {len(to_chat)}" + "".join(f"\n  - {q}" for q in to_chat))
    print(f"retry rate: {retried}/{len(cases)} (questions where validation rejected a reply)")
    print(f"model calls: {total_calls / len(cases):.2f} per question")
    print(f"latency: avg {sum(latencies) / len(latencies):.2f}s, worst {max(latencies):.2f}s")

    if held_out:
        ok = accuracy >= GATE_ACCURACY and not to_chat
        print(f"\nchat-feature gate (>= {GATE_ACCURACY:.0%} and 0 misroutes to chat): "
              f"{'PASS' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
