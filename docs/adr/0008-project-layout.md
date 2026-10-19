# ADR 0008: Project layout: an installable package, tests/, and evals/

- **Status:** Accepted
- **Date:** 2026-10-12

## Context

All the code lived flat in `src/`, and modules imported each other by bare name
(`import detection_assistant`, `from threads.camera import ...`). That worked
only because Python puts the folder of the script you run (`src/`) on its
import path. Two problems followed:

1. Nothing outside `src/` could import the code, so tests had nowhere to live,
   and the routing eval sat next to the application as if it were part of it.
2. There was no list of dependencies; setting up a machine (e.g. the Pi) meant
   copying a `pip install` line from the README.

We also needed to say what the routing eval *is*. It isn't a unit test: it
calls a model, takes minutes, and gives a score that moves by about ±1
question between runs. A unit test is deterministic, needs no services, and
passes or fails in seconds.

## Decision

```
pyproject.toml   package metadata, dependencies, pytest config
src/iot_lab/     the application: the only code that ships
tests/           pytest unit tests: deterministic, no Ollama, no camera, no network
evals/           model scoring: needs Ollama, reports a score, judged by a person
```

- **`src` layout with one package, `iot_lab`.** Imports are absolute
  (`from iot_lab.threads.camera import CameraHandler`). Programs run as modules:
  `python -m iot_lab.main`, `python -m iot_lab.detection_assistant`.
- **`pip install -e ".[dev]"`** installs the package editable (edits apply
  without reinstalling), its runtime dependencies, and the `dev` extras
  (pytest).
- **Unit tests vs. evals.** Deterministic logic (validation, guards, SQL views,
  payload format) gets a unit test in `tests/`; a plain `pytest` run must stay
  fast and never need Ollama. Model behaviour (routing, and phrasing later) gets
  cases in `evals/`, run before and after prompt or model changes (ADR 0004).

Rejected:
- **`PYTHONPATH=src` / pytest's `pythonpath` setting.** A smaller diff, but
  every script outside `src/` would depend on an environment variable, and
  nothing would declare the dependencies.
- **Flat modules installed as top-level names** (`main`, `threads`,
  `sqlite_handler`): generic names that can clash with other installed
  packages, and setuptools can't discover loose modules without listing each
  one by hand.
- **Flat layout (`iot_lab/` at the repo root).** Also common, but tests run
  from the root could import the local folder instead of the installed
  package, hiding packaging mistakes. `src/` prevents that.
- **The eval inside `tests/`.** It would sit beside fast pass/fail tests while
  needing a model server and producing a score.

## Consequences

- Accepted ADRs and `docs/sqlite-logging.md` keep the old `src/...` paths; they
  record what was true when written.
- `pyproject.toml` lists dependency names, not pinned versions. If the Pi needs
  exactly the laptop's versions, add a lock file (e.g. `pip freeze`).
- `ncnn_export.py` now anchors its paths to the code's location, like
  `main.py`, since running it as a module made `../models/...` meaningless.
