# ADR 0010: Document retrieval (RAG) for how-to questions

- **Status:** Accepted
- **Date:** 2026-11-09
- **Extends:** [ADR 0003](0003-detection-assistant.md) (the assistant: model
  routes, code computes, model phrases) and ADR 0004-0007 (structured routing).

## Context

The assistant answers questions about live camera DATA (counts, recent visits,
what's in view). It cannot answer how-to / design questions about the system
itself -- "where should the temperature sensor go?", "how does store-and-forward
survive an outage?" -- which currently fall through to `unknown`/`unclear`. The
answers already exist, in our own docs (README, CLAUDE.md, the ADRs).

Keyword search over those docs is brittle: "sensor overheating" wouldn't match
"device running hot". We want search by meaning.

## Decision

Add **retrieval-augmented generation (RAG)** as a new router intent, `docs`,
reusing the assistant's existing shape -- the model routes, code retrieves, the
model phrases:

1. **A new `docs` intent** (`detection_assistant.py`). The router sends how-to /
   design questions about the system to `docs`; data questions still go to the
   detection functions. `docs` takes no args (it uses the raw question).
2. **Retrieval in code** (`iot_lab/rag.py`, deterministic and testable):
   - **Chunk** the project docs (README, CLAUDE.md, `docs/**`) by paragraph,
     each chunk carrying its heading for context.
   - **Embed** each chunk with Ollama `nomic-embed-text` (local, no cloud).
   - **Store** the vectors in **ChromaDB**, local/embedded, persisted under
     `logs/chroma/` (gitignored, like `events.db`). Chosen over Pinecone/
     Weaviate/Qdrant/pgvector because it runs in-process on the Pi with no
     server or cloud account (the "local / embedded" option for prototyping).
   - **Retrieve** at query time: embed the question, cosine nearest-neighbour,
     return the top passages.
3. **Phrasing with a citation** (`phrase_docs`): the model answers in 1-3
   sentences from ONLY the retrieved passages, citing the source file. Same
   discipline as the data path -- the model phrases, it doesn't invent.
4. **Local only.** Embeddings and the vector store run on the Pi; no new AWS
   setup. The index is rebuilt with `python -m iot_lab.rag build`.

## Consequences

- `nomic-embed-text` is a second Ollama model to pull (~275 MB), separate from
  the chat model. ChromaDB is a new optional dependency (`pip install
  iot-lab[rag]`); it is imported lazily, so the assistant still runs for data
  questions without it.
- The index is a snapshot: after editing docs, re-run `rag build`.
- **Known limitation:** the code guard (ADR 0005) runs before routing and
  declines questions containing "how long" / "duration" as unsupported (it is
  tuned for the detection log). A how-to question phrased that way ("how long
  does the forwarder wait?") is declined as `unclear` rather than reaching
  `docs`. Rephrasing avoids it; narrowing the guard per-intent is future work.
- RAG is built as a standalone `retrieve()`, so it is reusable as a tool when
  the assistant becomes a LangGraph agent (future ADR).
