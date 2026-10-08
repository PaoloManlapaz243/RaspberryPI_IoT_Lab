# Detection Assistant: Question to Reply

How `src/detection_assistant.py` turns a typed question into a printed reply.
The decisions behind each step are in ADRs
[0003](adr/0003-detection-assistant.md) (separate read-only process, the model
routes and phrases),
[0004](adr/0004-structured-model-output.md) (constrain, validate, retry),
[0005](adr/0005-routing-evaluation.md) (unclear intent, code guard),
[0006](adr/0006-label-vocabulary-in-schema.md) (label vocabulary in the
schema), and [0007](adr/0007-per-function-schema-and-label-check.md)
(per-function schema, second-label check).

```mermaid
flowchart TD
    A["You type a question<br/>input() in the REPL"] --> B{"Guard the question<br/>clock time, past day, 2+ labels?"}
    B -- no --> C["_ask_model()<br/>Ollama + per-function schema"]
    C --> D{"validate_call()<br/>valid reply?"}
    D -- "no: retry once" --> C
    D -- yes --> E{"Check the answer<br/>2nd label, unclear or unknown?"}
    E -- no --> F["execute()<br/>read-only SQLite query"]
    F --> G["phrase()<br/>Ollama writes one sentence"]
    G --> H["Print the reply<br/>log_chat if on, then next prompt"]

    B -- yes --> X["Decline<br/>unclear, unknown, or invalid<br/>fixed message, no data read"]
    D -- "still invalid, or<br/>not_a_detector_label" --> X
    E -- yes --> X
    X --> H

    classDef io fill:#F1EFE8,stroke:#5F5E5A,color:#2C2C2A
    classDef routing fill:#EEEDFE,stroke:#534AB7,color:#26215C
    classDef data fill:#E1F5EE,stroke:#0F6E56,color:#04342C
    classDef decline fill:#FAECE7,stroke:#993C1D,color:#4A1B0C
    class A,H io
    class B,C,D,E routing
    class F,G data
    class X decline
```

## Reading the diagram

| Color | Meaning |
|---|---|
| Gray | Terminal input and output |
| Purple | Routing: the model is surrounded by code checks before and after |
| Teal | Data path: code answers the question, and the model only phrases the result |
| Coral | Decline: a fixed message; nothing is read and nothing is made up |

- **At most two model calls per question:** `_ask_model()` (twice if
  `validate_call()` forces a retry) and `phrase()`. A question the guard
  declines makes **no** model calls.
- **Where it happens in `route()`:** guard the question
  (`unsupported_features()`) → `_ask_model()` → guard the answer (a second
  label named in the question → `unclear`). `answer()` then declines
  `unclear`, `unknown`, and `invalid` with fixed messages.
- **Errors aren't on this path:** if Ollama is unreachable or `logs/events.db`
  is missing, the REPL loop prints a hint and returns to the prompt.

Keep this diagram in sync with `route()` and `answer()`: update it in the same
commit as any change to the flow.
