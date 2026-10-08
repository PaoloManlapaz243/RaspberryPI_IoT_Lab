# ADR 0006: Detector vocabulary in the schema, not a synonym table

- **Status:** Accepted
- **Date:** 2026-10-08
- **Supersedes:** the `LABEL_SYNONYMS` part of [ADR 0005](0005-routing-evaluation.md)

## Context

ADR 0005 mapped everyday words to detector labels with a hand-written table
(`"bike" → bicycle`, `"phone" → cell phone`). That tied the assistant's quality
to one model's COCO naming and to whatever words someone remembered to list.
The detector vocabulary itself was already loaded from the exported model's
`metadata.yaml`, so it follows the model automatically.

## Decision

Put the vocabulary in the router's JSON schema, the "constrain" layer of
ADR 0004:

- `label` is an enum: `sorted(DETECTOR_LABELS) + ["not_a_detector_label", null]`.
  The model can only name a real label, and does the word → label mapping
  itself ("pedestrians" → person).
- `not_a_detector_label` is the escape value for categories or things the
  detector can't see. Without it, a forced choice could silently pick a wrong
  label. `validate_call()` turns it into `unclear` without spending a retry.
- The router prompt lists the labels (generated, not hand-written) and has one
  mapping example.
- `canonical_label()` keeps only model-agnostic rules (case, simple plurals).
  `LABEL_SYNONYMS` is removed.

Rejected alternatives: embedding similarity (another model on the Pi, and
confident near misses such as vehicle ≈ car); moving the table to a per-model
config file (fixes the style, not the coupling); keeping the table.

## Comparison (gemma2:2b, guard on, laptop)

The held-out set gained 10 questions before any run: 8 synonyms the table
never listed, and 2 categories that must decline.

| | Synonym table | Schema vocabulary (chosen) |
|---|---|---|
| Held-out overall | 31/40 (78%) | **36/40 (90%)** |
| Held-out: word had to be mapped | 4/11 | **11/11** |
| Held-out: questions it should decline | 7/7 | 7/7 |
| Tuning overall | **45/50 (90%)** | 40/50 (80%) |
| Tuning: questions it should decline | **6/7** | 4/7 |
| Misroutes to chat | 0 | 0 |
| Held-out: questions needing a retry | 11/40 | **1/40** |

The table wins on words it lists; the schema wins on words it doesn't. The
held-out additions target that difference by design.

## Known regressions (open)

1. **`recent` loses `n`:** "what was the last thing you saw?" →
   `recent()` → 5 visits instead of 1 (4 eval cases). Not label-related: the
   prompt grew by the 80-label list, and the model began leaving out `n`.
2. **The guard misses "people and dogs":** its multi-label check used the
   table to recognize "people". Now that question reaches the model, which
   answers `count(person)`.

Also observed: **"how many vehicles passed?" → `truck`**, the forced-choice
risk. The model used the escape value for "electronics" and "furniture", but
not here.

## Consequences

- Swapping the detector updates the assistant's vocabulary with no code
  change.
- Word mapping is now probabilistic. The eval's synonym sub-score measures it.
- The prompt is ~80 labels longer. Ollama caches the system prompt, so latency
  barely changed on the laptop; re-check on the Pi.
