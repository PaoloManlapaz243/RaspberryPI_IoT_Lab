# ADR 0005: Routing evaluation, the `unclear` intent, and a code guard

- **Status:** Accepted
- **Date:** 2026-10-08
- **Builds on:** [ADR 0004](0004-structured-model-output.md) (constrain,
  validate, measure)

## Context

We want a restricted off-topic chat mode in the assistant. Its risk is a
detection question being misrouted to chat, where a model with no data
invents an answer. Telling the chat prompt "don't talk about detections" is
only a backup; the real protection is routing. Before building chat, routing
had to be measurably good. **Constraint: the assistant runs on the Raspberry
Pi next to YOLO**, so bigger models were ruled out.

The old eval (20 tuning + 8 held-out) couldn't measure progress: one held-out
question was 12.5%.

## Decision

1. **A larger eval** (`src/eval_assistant.py`): 50 tuning / 30 held-out
   questions, written before any tuning. It covers negation, synonyms, typos,
   filler, unsupported times, multi-label and attribute questions. It reports
   a confusion table, **misroutes to chat** (the safety number), the retry
   rate, model calls per question, and latency.
2. **An `unclear` intent**: about detections, but no function answers it
   (clock times, past days, several labels, durations, attributes). It never
   reads the DB and never goes to chat. `answer()` explains what *can* be
   asked.
3. **Labels are validated against the detector's vocabulary** (the 80 labels
   in the exported model's `metadata.yaml`) via `canonical_label()`, with an
   unambiguous synonym table ("humans" → person, "bike" → bicycle). A
   category like "vehicles" is rejected instead of silently counting 0.
   This replaces the DB-based `_normalize_label()`, so normalization lives in
   one place.
4. **A code guard** (`unsupported_features()`, `USE_GUARD = True`): regexes for
   clock times, parts of the day, past days, and "how long", plus 2+ detector
   labels named in the question → `unclear` **before any model call**.
5. **A gate for the chat feature:** held-out accuracy ≥ 90% **and** 0 detection
   questions routed to chat. Accuracy covers visible, recoverable confusions
   between functions; the zero bar covers made-up answers.

## Why a code guard

The baseline showed the worst failure wasn't misrouting to chat. It was
**`unclear` questions forced into a function that answers a different
question with real data**: "since 9am" → `count(…, 1440)`, "people and dogs" →
`count(person)`. Prompt rules ("use unclear for clock times") didn't change
this: a 2B model reads the rule and ignores it. Code can't be talked out of a
rule. It also skips the model entirely, which matters on the Pi.

Its limit: it only catches the patterns it lists. A miss falls back to the
model, not to something worse.

## Results (gemma2:2b, temperature 0, laptop)

Strategy chosen **by tuning score, ties by fewest model calls**, a rule fixed
before running. Held-out only confirmed it.

| Strategy | Tuning | Held-out | To chat | Calls per question |
|---|---|---|---|---|
| Baseline (ADR 0004 router) | 39/50 (78%) | 24/30 (80%) | 1 | ~1.1 |
| + `unclear`, label validation, prompt | 41/50 (82%) | 23/30 (77%) | 0 | ~1.1 |
| **+ code guard (chosen)** | **45/50 (90%)** | **27/30 (90%)** | **0** | **~1.0** |
| Two-stage routing | 32/50 (64%) | 19/30 (63%) | 1–2 | 1.6 |
| Two-stage + guard | 35/50 (70%) | 22/30 (73%) | 1–2 | 1.4–1.5 |

**Gate: PASS** (held-out 90%, 0 to chat).

## Rejected: two-stage routing

Call 1 picked the intent, call 2 extracted args with a per-function schema. It
was built and measured, and was clearly worse. The argument step, seeing only
"extract since_minutes", **invented time windows** ("how many dogs were
there?" → 30; "past 3 hours" → 360), once swapped a label (cat → dog), and
sent detection questions to chat. Splitting the task removed the context a
small model needed. The code was removed; these numbers are the record.

Also rejected: bigger models (Pi RAM/CPU), and sampling several answers and
voting (multiplies latency on the Pi).

## Caveats

- **90% is exactly the threshold.** With 30 questions, one question is 3.3%.
- **Possible leakage:** `present_now`'s "including whether it is empty" was
  written knowing a held-out question ("is the room empty?") failed there.
- **A silent wrong answer remains:** "how many folks stopped by…" → the model
  dropped the label and counted everything. The synonym table only helps when
  the model passes the word through.
- **Laptop numbers.** Re-run `python src/eval_assistant.py --held-out` on the
  Pi with `main.py` running before relying on the latency figures.
- The string `"null"` is normalized to null in `validate_call()` (found while
  testing two-stage; unambiguous, so it's not a guess).

## Consequences

- The chat feature may start: route only `unknown` to it, and keep reporting
  misroutes to chat after it ships.
- Adding a function: also consider whether `UNSUPPORTED_PATTERNS` or
  `LABEL_SYNONYMS` need updating, and add eval cases.
- Retuning the prompt must be checked on held-out with the guard on, and
  without it (`--no-guard`), so the guard doesn't hide a regression.
