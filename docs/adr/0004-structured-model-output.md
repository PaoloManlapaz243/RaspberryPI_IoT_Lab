# ADR 0004: Constrain, validate, and measure model output

- **Status:** Accepted
- **Date:** 2026-10-08
- **Builds on:** [ADR 0003](0003-detection-assistant.md) (the model routes and
  phrases, code computes)

## Context

The assistant's router asks a small local model (`gemma2:2b` via Ollama) to turn
a question into `{"function", "args"}`. In live use it returned
`"function": "present_now()"` and `"recent(1)"` (copying the prompt's
`name(args)` notation), and `since_minutes: 0` where the prompt said "null".
`format: "json"` only guaranteed valid JSON, not this structure.

Two fixes were considered and rejected on their own:

| Approach | Why not, alone |
|---|---|
| **More prompt rules** | Only make good output more likely. The model had just broken an explicit rule. |
| **Repair in code** (strip parentheses, map 0 → null, ...) | Only handles mistakes already seen; the model's output space is unbounded. A wrong guess silently runs the wrong query, which is worse than an error. |

## Decision

Four layers, each covering what the others can't:

1. **Constrain (`ROUTER_SCHEMA`).** Send a JSON schema as Ollama's `format`. It
   is enforced during generation, so malformed names and types can't be
   produced. Function names come from `FUNCTIONS`, so the schema can't drift
   from the code.
2. **Validate (`validate_call`), and reject rather than guess.** A schema checks
   format, not meaning. One set of rules (`ALLOWED_ARGS`, `REQUIRED_ARGS`, value
   ranges) checks arguments: null means "not given", and anything else wrong
   raises `InvalidCall` with a reason. `route()` retries **once**, showing the
   model its reply and the reason. If that fails, the user is asked to rephrase.
   `execute()` only ever sees validated calls.
3. **Prompt examples for meaning.** Choices a schema can't check ("no time
   window → null", "how many → count") are taught with worked examples, which
   small models follow better than prose rules.
4. **Measure (`src/eval_assistant.py`).** A tuning set (`CASES`) and a
   **held-out** set (`HELD_OUT`) that is never tuned against. Every prompt or
   model change is judged by its score, not by feel.

Also: `last_seen`'s hidden fallback (no label → `recent(1)`) became a
validation rule (`REQUIRED_ARGS`). The retry then routes to `recent`, instead
of a data function silently guessing.

## Results (2026-10-08, gemma2:2b, temperature 0)

| Set | Before | After |
|---|---|---|
| Tuning (20) | 12/20 | 20/20 |
| Held-out (8) | — | 7/8 |
| `llama3.2:1b`, tuning set | — | 12/20, so `gemma2:2b` stays |

The tuning score is optimistic, because rules were written while looking at its
failures. The held-out score is the honest estimate.

## Consequences

- Needs an Ollama version with JSON-schema `format` support. 0.40.1 is
  verified.
- A retry costs one extra model call (~0.3 s) on invalid replies.
- Adding a function now means updating `FUNCTIONS`, `ALLOWED_ARGS` (an assert
  enforces this), the prompt, and eval cases.
- This mirrors how LangChain/LangGraph define tools (argument schemas +
  validation), so the functions are already shaped as tool definitions.
- The held-out set must be refreshed whenever one of its failures is used for
  tuning.
