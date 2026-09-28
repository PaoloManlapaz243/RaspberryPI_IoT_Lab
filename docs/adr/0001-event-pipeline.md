# ADR 0001: Event pipeline for detection logging

- **Status:** Accepted
- **Date:** 2026-09-28

## Context

Inference produces detection events that must be stored locally (SQLite,
replacing TinyDB) and later published to AWS IoT Core. The long-term goal is
to add agentic features (LangChain / LangGraph) that read this data, so the
database must be both fresh and able to answer "what is in view right now?"
as well as historical questions.

Concerns raised:

- Buffered writes would leave the newest events invisible to an agent.
- Doing I/O on the inference thread stalls frames (SQLite fsync on an SD card
  can take tens of milliseconds; MQTT can block on the network).
- Today only the first sighting of each track is logged. `last_seen` lives only
  in memory and nothing records an object leaving, so the database cannot
  reconstruct current state. `tracked_objects` also grows without bound.

The old Experiment 1 design batched writes every 5 s (`memory_queue`). The
current `queue.Queue` is **not** a batch buffer: it is a thread hand-off that
adds microseconds, compared with ~100 ms+ per inference frame on the Pi.

## Decision

**Option A: keep the queue, write each event immediately, and log the full life of each track.**

```
CameraHandler -> InferenceHandler --queue.Queue--> Logging thread -> SQLite (WAL)
                  (tracks enter/exit)
```

1. **Keep `queue.Queue`** between inference and logging. The queue keeps
   inference isolated from disk and network latency.
2. **Write each event as soon as it arrives.** The logging thread writes each
   event as it is dequeued. No time-based batching.
3. **Log the full life of each track** with two event types:
   - `enter`: the first time a track ID is seen.
   - `exit`: the track hasn't been seen for N seconds. The same sweep deletes
     the ID from `tracked_objects`, which bounds its memory.
4. **Use plain Python values in events** (no numpy types), and include
   `track_id`. The same dict can be written to SQLite and published over MQTT
   without conversion.
5. **Put SQLite in WAL mode** so readers (a future agent) can query while the
   logging thread writes.
6. **The logging thread owns its SQLite connection.** It opens the connection
   inside its task, because `sqlite3` connections are tied to the thread that
   created them. It uses `queue.get(timeout=...)` so it notices shutdown, and
   writes whatever is still queued before exiting.

## Future direction: Option C (event bus)

Later, the single queue becomes an event bus that fans each event out to
several independent consumers:

- SQLite writer (history)
- MQTT publisher to AWS IoT Core
- Agent trigger that starts a LangGraph run when an event matches a rule
  (for example, "person detected after midnight").

To keep that step small, Option A should:

- Keep producers unaware of their consumers: inference only puts events on a
  queue and never calls the DB or AWS directly.
- Use one event format shared by every consumer (decision item 4).
- Give each consumer its own thread and queue, so a slow consumer (network)
  can't delay the others.

## Alternatives considered

| Option | Why not |
|---|---|
| **B: write directly from the inference thread (no queue)** | Simpler, but disk I/O and MQTT latency stall inference. Adding MQTT later would bring the queue back anyway. |
| **Time-batched writes** (Experiment 1 style) | Data up to one interval stale; unwritten events lost on a crash. |
| **Log first sighting only** (current) | Database can't answer "what's in view now" or "how long was it present". |

## Consequences

- The database can reconstruct who was present at any moment from enter/exit
  events.
- The size of `tracked_objects` is bounded by what is currently in view.
- One extra thread to start and shut down cleanly.
- The exit timeout (N seconds) is a tuning parameter. Too short and tracker
  flicker produces duplicate enter/exit pairs; too long and exits are late.

## Open questions

- **Where does the agent run?** Same process on the Pi, a separate process on
  the Pi (SQLite as the shared layer), or the cloud (DynamoDB as the source).
  A separate process on the Pi is the current leaning.
- **Reactive, conversational, or both?** This decides when the agent-trigger
  consumer from Option C is needed.
