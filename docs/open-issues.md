# Open Issues

Living list of known problems, pending decisions, and next steps. Last updated
2026-10-08. Remove an item in the same commit that resolves it.

## Current state (2026-10-08)

- `main` is up to date with GitHub and includes PRs #1–#6 plus the assistant
  flowchart (`docs/assistant-flow.md`). No open branches of ours.
- Detector pipeline: SQLite event log + store-and-forward to AWS (ADRs
  0001–0002).
- Assistant: separate read-only process, gemma2:2b via Ollama (ADRs 0003–0007).
  Routing eval: tuning 47/52, held-out 38/45 (84%), 0 misroutes to chat
  (laptop numbers).

## Decisions pending

1. **Chat-feature gate.** ADR 0005 set it to held-out ≥ 90% and 0 misroutes to
   chat. Held-out is now 84% on the larger, harder 45-question set (it was 90%
   on the original 30). Options: improve routing, re-baseline the gate (decide
   before measuring again), or start chat with only the safety half of the
   gate. See ADR 0007.
2. **Ben's later assistant work isn't ported.** `origin/BenIoT` commit `c3d10da`
   ("Updated Assistant", 2026-09-28) added `stats(op, label, since_minutes)`
   (avg/min/max detection confidence) and `busiest(since_minutes)` (busiest
   hour). Our port was based on his earlier `2d045a4`. Decide with Ben whether
   to port them. Each new function needs the ADR 0004–0007 treatment: schema
   branch, `ALLOWED_ARGS`, prompt, eval cases. `busiest` touches clock hours,
   which the guard currently declines. Then retire `BenIoT`.

## Assistant: routing and answers

3. **Categories forced onto one label.** "pets" → cat, "electronics" → cell
   phone, "vehicles" → truck. The model doesn't reliably use
   `not_a_detector_label`. A prompt example was tried and rejected (ADR 0007).
   The real fix is a feature: a category maps to several labels.
4. **Both-sides synonym multi-object questions.** "pups or kittens" →
   `count(cat)`. The post-routing check only sees literal label words.
5. **Off-topic routed to `unclear`.** "good morning", "what time is it?" →
   `unclear`. This is safe (it declines), but costs accuracy. It matters once
   the chat feature exists.
6. **Empty-room phrasing.** `present_now()` with nothing in view produces
   "Nothing matching was found." It should read "No one is in view."
7. **No phrasing eval.** `eval_assistant.py` only measures routing. A
   phrasing bug (null → "Nothing matching was found") shipped in PR #4
   because of this. Idea: check that the number from the data appears in
   the sentence.
8. **"today" means the last 24 hours**, not since midnight.
9. **Not measured on the Pi.** Run `python src/eval_assistant.py --held-out` on
   the Pi with `main.py` running (latency and accuracy under YOLO load). Ollama
   and gemma2:2b must be installed there.

## Detector pipeline

10. **Tracker overcount.** Each time the tracker loses a person, they count as
    a new visit (e.g. "77 people"). `EXIT_TIMEOUT_S = 2.0` in
    `src/threads/object_detection.py` is untuned. This is the most visible
    accuracy problem in the assistant's answers.
11. **Exits only fire while frames arrive.** `_sweep_exits()` runs once per
    inference frame, so if the camera stalls, exits wait until shutdown (their
    timestamps are still correct).
12. **Event `ts` is when inference finishes, not frame capture** (possibly
    100–300 ms late on the Pi). Fix: capture the time in the camera thread with
    the frame.
13. **Stale "present" after a crash.** If `main.py` dies without 'q' or Ctrl+C,
    the last visits never get exits, and `present_now()` shows them until the
    next run starts.
14. **`CameraHandler` calls `exit()` in its constructor** (`src/threads/camera.py`);
    it should raise. Also, width/height defaults are portrait (480×640).
15. **`ncnn_export.py` uses paths relative to `src/`** (`../models/yolo11n.pt`).
    Anchor them to the file like `main.py` does.

## AWS

16. **DynamoDB table pending** (group mate). The spec is in
    `docs/aws-setup.md`: partition key `sensor_id`, sort key `event_key`.
    The broker has acknowledged uploads, but items landing in DynamoDB isn't
    verified.
17. **Chat logs** need their own table, rule, and policy (`chat/logs`, client
    `assistant-dev`). `LOG_CHAT_TO_AWS = False` until then. The chat
    `timestamp` is local time, unlike events (UTC).

## Housekeeping

18. **Commented-out old `AWSPublisher` class** at the top of
    `src/aws_publisher.py` (~47 lines). Delete it; git history keeps it.
19. **Stray `~/Desktop/models/`** outside the repo (an old relative-path
    export). Unused; delete after checking nothing needs it.

## Next up (suggested order)

1. Decide #1 (gate) and #2 (Ben's functions) with the group.
2. #10 tracker tuning: it affects every count the assistant reports.
3. #9 Pi measurement.
4. Off-topic chat feature (once #1 is settled), then the LangGraph work
   (the `langraph` branch was empty and is gone).
