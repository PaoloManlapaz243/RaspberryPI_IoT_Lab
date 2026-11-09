"""
Tool-selection eval for the LangGraph agent (docs/adr/0011-langgraph-agent.md).

Runs each question through the whole agent graph with the real model and checks
WHICH TOOLS it called, not the wording of the answer. A case passes when every
`must` tool was called and no `never` tool was. Data comes from logs/events.db
as it is; the score is about tool choice, so the data's content doesn't matter
(an empty or missing log just makes the tools return "none" or an error).

Run (Ollama must be running; same two-set rule as eval_assistant.py: tune on
CASES, only ever CHECK with HELD_OUT):
  python evals/eval_agent.py                 # tuning set
  python evals/eval_agent.py qwen2.5:3b      # another model
  python evals/eval_agent.py --held-out
  python evals/eval_agent.py --verbose       # print passing cases too

Safety metric reported separately: "answered off-topic with data". An off-topic
question must call no tool at all.
"""

import sys
import time

from iot_lab import agent

#(question, must call these tools, never call these). Args aren't checked here;
#eval_assistant.py measures argument accuracy for the shared detection tools.
CASES = [
    # --- one tool
    ("is anyone in front of the camera?",                  {"present_now"},              set()),
    ("how many people came by in the last hour?",          {"count"},                    set()),
    ("when did you last see a dog?",                       {"last_seen"},                set()),
    ("what showed up most often today?",                   {"most_common"},              set()),
    ("what were the last 3 things you saw?",               {"recent"},                   set()),
    ("what's the temperature right now?",                  {"latest_reading"},           set()),
    ("how humid is it?",                                   {"latest_reading"},           set()),
    ("how do I wire the DHT11 to the Pi?",                 {"docs"},                     set()),
    ("why does the forwarder keep a cursor in SQLite?",    {"docs"},                     set()),
    # --- several tools for one question
    ("is it hot and is anyone here?",                      {"latest_reading", "present_now"}, set()),
    ("how many people and how many dogs came by today?",   {"count"},                    set()),
    ("what's the humidity, and what did you see last?",    {"latest_reading", "recent"}, set()),
    # --- the per-tool guard: a how-to question with "how long" reaches docs
    ("how long does the forwarder wait between batches?",  {"docs"},                     set()),
    # --- off-topic: no tool at all
    ("tell me a joke",                                     set(),                        agent.TOOL_ARGS.keys()),
    ("what's the capital of France?",                      set(),                        agent.TOOL_ARGS.keys()),
    ("hello!",                                             set(),                        agent.TOOL_ARGS.keys()),
]

#Written 2026-11-09 before any run. Never tune against these.
HELD_OUT = [
    ("anybody around at the moment?",                      {"present_now"},              set()),
    ("total number of cars in the past 2 hours?",          {"count"},                    set()),
    ("is it warm in the room?",                            {"latest_reading"},           set()),
    ("where is the DynamoDB table set up?",                {"docs"},                     set()),
    ("is the room occupied and how warm is it?",           {"present_now", "latest_reading"}, set()),
    ("what is event_key used for?",                        {"docs"},                     set()),
    ("recommend a good book",                              set(),                        agent.TOOL_ARGS.keys()),
    ("what's 12 times 7?",                                 set(),                        agent.TOOL_ARGS.keys()),
]


def tools_called(state):
    return [s["call"]["function"] for s in state.get("steps", [])]

def check(called, must, never):
    called = set(called)
    missing, forbidden = set(must) - called, called & set(never)
    if missing:
        return f"missing {sorted(missing)}"
    if forbidden:
        return f"called {sorted(forbidden)}"
    return None


def _instrument():
    #Count model calls per question without changing the agent
    counts = {"calls": 0}
    real = agent.ollama_llm
    def llm(*a, **k):
        counts["calls"] += 1
        return real(*a, **k)
    return llm, counts


def main():
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    models = [a for a in sys.argv[1:] if not a.startswith("--")]
    if models:
        agent.MODEL = models[0]
    verbose = "--verbose" in flags
    held_out = "--held-out" in flags
    cases, name = (HELD_OUT, "held-out") if held_out else (CASES, "tuning")
    print(f"model={agent.MODEL}  set={name}  cases={len(cases)}  max_steps={agent.MAX_STEPS}\n")

    llm, counts = _instrument()
    graph = agent.build_graph(llm=llm)
    passed, off_topic_with_data, budget_hits, total_calls, latencies = 0, [], 0, 0, []
    for question, must, never in cases:
        counts["calls"] = 0
        start = time.time()
        state = agent.run(question, graph=graph)
        latencies.append(time.time() - start)
        total_calls += counts["calls"]

        called = tools_called(state)
        reason = check(called, must, never)
        passed += reason is None
        budget_hits += len(called) >= agent.MAX_STEPS
        if not must and called:
            off_topic_with_data.append(question)

        if reason is not None or verbose:
            mark = "PASS" if reason is None else f"FAIL ({reason})"
            print(f"{mark:<34} {question}\n{'':<34} -> {called}  {state['answer']!r}")

    print(f"\nscore: {passed}/{len(cases)} ({passed / len(cases):.0%})")
    print(f"off-topic answered with data: {len(off_topic_with_data)}"
          + "".join(f"\n  - {q}" for q in off_topic_with_data))
    print(f"hit the step budget: {budget_hits}/{len(cases)}")
    print(f"model calls: {total_calls / len(cases):.2f} per question")
    print(f"latency: avg {sum(latencies) / len(latencies):.2f}s, worst {max(latencies):.2f}s")


if __name__ == "__main__":
    main()
