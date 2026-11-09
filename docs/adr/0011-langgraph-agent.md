# ADR 0011: LangGraph agent over the assistant's tools

- **Status:** Accepted
- **Date:** 2026-11-09
- **Extends:** [ADR 0003](0003-detection-assistant.md) (model routes, code
  computes, model phrases), ADRs 0004-0007 (constrained, validated routing),
  and [ADR 0010](0010-doc-retrieval.md) (doc retrieval). Does not replace any
  of them: `detection_assistant.py` keeps its router.

## Context

The assistant routes **one** question to **one** function. That was right for
counting visits, but the next goals need more:

- **Multi-part questions:** "is it hot *and* is anyone here?" needs a sensor
  reading and the camera, then an answer that combines them. The router
  declines two-part questions because one call would drop half (ADR 0007).
- **One entry point** for data questions, how-to questions (RAG) and, next,
  commands to the ESP32 (ADR 0012).
- The goal stated in ADR 0003: agentic features with LangChain/LangGraph,
  reusing the assistant's functions as tools.

## Options considered

| Option | Summary | Verdict |
|---|---|---|
| **A. Extend the hand-rolled router** | Allow lists of calls in the router schema | Rejected. Every later tool needs results from the earlier ones (a reading, then a decision), so it is a loop in all but name. Hand-writing that loop, its budget and its state is what LangGraph provides. |
| **B. LangGraph prebuilt ReAct agent + `ChatOllama.bind_tools`** | Native tool calling | Rejected. gemma2:2b (the settled chat model) has no tool-calling support in Ollama; it would force a model change. Native tool args are also unconstrained, losing ADR 0004's schema guarantee. |
| **C. Explicit LangGraph state machine, JSON-schema planner** | Our own nodes; the planner emits one `{"function", "args"}` per turn under a schema, as the router does | **Chosen.** Works with gemma2:2b, keeps constrained decoding + code validation, and every safety rule is a visible node or edge. |

**Alongside, not instead of, the assistant** (decided with the team). The
router stays the fast, measured path for single questions; the agent is the
richer path. They share the tool functions, so neither can drift. If the
agent proves to cover everything, retiring the router is a later decision.

## Decision

1. **New module `iot_lab/agent.py`**, run with `python -m iot_lab.agent`.
   Optional extra `agent = ["langgraph", "langchain-core", "langchain-ollama"]`
   (`langchain-core` instead of the full `langchain` package: only messages
   and the Ollama binding are used).
2. **Tools wrap existing code; nothing is reimplemented.** The six detection
   functions are `detection_assistant.FUNCTIONS` with its arg schemas and
   `validate_call()`. `docs` calls `rag.retrieve()` on the user's own
   question. One new read-only tool, `latest_reading()`, returns the newest
   reading per sensor from `readings` (ADR 0009) through the assistant's
   read-only `_query()`.
3. **The graph:**

   ```mermaid
   flowchart LR
       G["guard_input<br/>empty / too long"] --> P["plan<br/>model + schema"]
       P --> C{"check<br/>allow-list, validate,<br/>guard, dedupe"}
       C -- act --> A["act<br/>run the tool"]
       A -- "steps < MAX" --> P
       C -- "rejected: retry<br/>with the reason" --> P
       C -- "finish / budget" --> R["respond<br/>phrase from data,<br/>or fixed text"]
       A -- "steps = MAX" --> R
   ```

   - `plan`: the model gets the question plus all results so far, and must
     answer with one schema branch: a tool with its exact args, or `finish`.
   - `check` (the safety node): a hard allow-list in code (independent of
     the schema), `validate_call()` for detection args, the clock-time /
     duration guard **per detection tool**, and rejection of a repeated
     identical call. A rejection goes back to `plan` with the reason (as in
     ADR 0004's retry); a decline (e.g. "since 9am") is recorded as a result,
     so the answer says that part can't be answered.
   - `respond`: no tool ran → fixed text, no model call (off-topic, or gave
     up). Every result declined/errored → fixed text listing the reasons.
     Otherwise the model phrases 1-3 sentences from the results only, citing
     doc sources. If the question names a detector label no call looked up,
     code appends "(I didn't look up: dog.)".
4. **Bounded:** `MAX_STEPS = 4` tool calls and `MAX_PLAN_ERRORS = 2` rejected
   plans per question, plus LangGraph's `recursion_limit` as a backstop.
5. **Prompt-injection defense:** tool results (sensor values, retrieved doc
   text) reach the model only inside one `<data>` block, JSON-encoded with
   `<`/`>` escaped so text can't close or open the block, and both prompts say
   data is never an instruction. That alone is not trusted: structurally, the
   model can only emit allow-listed calls, and every check is made against
   the user's question, never against tool output.
6. **Measured:** `evals/eval_agent.py` scores tool choice (must-call /
   never-call per question) on a tuning and a held-out set, and reports
   off-topic questions answered with data, step-budget hits, model calls and
   latency. Argument accuracy for the shared detection tools stays in
   `eval_assistant.py`.

## Consequences

- **The per-tool guard fixes ADR 0010's known limitation on this path:** "how
  long does the forwarder wait?" can reach `docs`, because the clock/duration
  guard applies only to detection tools. The router still has the limitation.
- **Multi-object questions are answerable:** "people and dogs" can be two
  `count` calls instead of a decline. The code note keeps a dropped object
  visible rather than silently answered for one.
- **Cost:** at least two model calls per answered question (plan + finish, +
  phrase), one more per extra tool. Slower than the router on the Pi; not yet
  measured there.
- **Not yet measured at all:** the eval cases were written before any run.
  The router's gate (ADR 0005) does not apply to the agent; whether the agent
  needs its own gate is open.
- The `command` tool (ADR 0012) plugs in as one more entry in `TOOL_ARGS`
  plus a check in `validate()`; the graph does not change.
