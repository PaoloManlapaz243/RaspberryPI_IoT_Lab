# ADR 0003: Detection assistant as a separate read-only process

- **Status:** Accepted
- **Date:** 2026-09-28
- **Resolves:** the "Where does the agent run?" open question in
  [ADR 0001](0001-event-pipeline.md).

## Context

Ben's Ollama assistant (BenIoT branch, commit `2d045a4`) answers
natural-language questions about detections. It was written against the old
TinyDB log (`camera_logs.json`, per-frame `detections`). `main` now stores
enter/exit events in SQLite (ADR 0001) and uploads them via store-and-forward
(ADR 0002). The long-term goal is agentic features built with
LangChain/LangGraph.

## Decision

1. **A separate process, reading the same SQLite file** (`logs/events.db`),
   opened **read-only** (`mode=ro`), so SQLite itself refuses any write. WAL
   mode (ADR 0001) lets it read while `main.py` writes. Rejected: running
   inside `main.py`. That would put the LLM calls and the Ollama client in the
   real-time vision process and compete with inference for the Pi's CPU.
2. **Keep Ben's pattern: deterministic functions + a model that only routes
   and phrases.** The model never computes numbers. This is the same shape as
   LLM tool calling, so these functions become LangGraph tools later with
   little change.
3. **Queries live in the assistant, not in `SQLiteHandler`.** `SQLiteHandler`
   creates the schema, so it writes; the assistant must never write.
4. **Counts are visits** (enter events), not per-frame detections.
   `present_now()` (open tracks in the latest `run_id`) is new; the old
   per-frame log couldn't answer it.
5. **Times are converted to local time in code** before the model sees them.
   The DB stores UTC; a small model would read UTC out as local time.
6. **Label normalization is done in code** ("People" → `person`), rather than
   trusting the prompt.
7. **Chat logs are published directly, not store-and-forward.** A chat log
   sent while offline is lost on exit. That's acceptable for chat history and
   avoids giving a read-only process a write path. Off by default
   (`LOG_CHAT_TO_AWS = False`) until the AWS side exists; when on, missing
   config fails at startup (the same rule as `main.py`).

## Consequences

- Two MQTT clients share the `detector-01` certificate, so they need distinct
  client IDs (`detector-01`, `assistant-dev`). The IoT policy must allow both.
- `present_now()` trusts that the latest run shut down cleanly. After a crash,
  that run's last visits look "present" until the next run starts.
- Ollama and the model must be installed wherever the assistant runs. On the
  Pi, a 2B model alongside YOLO may be slow or run short of memory; test on
  the laptop first.
- Model names are Ollama tags (`gemma2:2b`, `qwen2.5:3b`). Verify they still
  exist before pulling.
