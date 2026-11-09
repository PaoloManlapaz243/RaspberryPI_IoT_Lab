"""
Document retrieval for the assistant (docs/adr/0010-doc-retrieval.md).

The detection functions answer questions about live camera DATA. This module
answers how-to / design questions about the SYSTEM itself, from the project's
own docs (README, CLAUDE.md, the ADRs). It mirrors the assistant's split:
  - code retrieves the relevant passages (deterministic)
  - the model phrases an answer from them, with a citation (in detection_assistant)

Stack (all local, no cloud):
  - embeddings: Ollama `nomic-embed-text` (turns text into a vector)
  - vector store: ChromaDB, local/embedded (persisted under logs/chroma/)
  - retrieval: embed the question, nearest-neighbour search, return top passages

Run:
  ollama pull nomic-embed-text
  python -m iot_lab.rag build          # (re)build the index from the docs
  python -m iot_lab.rag "where should the temperature sensor go?"   # test a query
"""

import re
import requests
from pathlib import Path

# ---------------- config ----------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHROMA_DIR   = PROJECT_ROOT / "logs" / "chroma"   # gitignored, like events.db
COLLECTION   = "docs"

OLLAMA_EMBED_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL      = "nomic-embed-text"

MAX_CHARS = 900   # target chunk size; big enough for a whole idea, small enough to be specific


def ollama_embed(texts):
    """Embed each string with nomic-embed-text via Ollama. Returns a list of
    vectors. Separate from the chat model: an embedder measures meaning, it
    doesn't generate text."""
    vectors = []
    for text in texts:
        r = requests.post(OLLAMA_EMBED_URL, json={"model": EMBED_MODEL, "prompt": text}, timeout=120)
        r.raise_for_status()
        vectors.append(r.json()["embedding"])
    return vectors


def chunk_markdown(text, source, max_chars=MAX_CHARS):
    """Split a markdown doc into retrieval chunks. Paragraphs are packed up to
    max_chars, and each chunk is prefixed with the most recent heading so a
    passage carries its context ("Decision", "Consequences", ...)."""
    chunks, buf, buf_len, heading = [], [], 0, ""

    def flush():
        nonlocal buf, buf_len
        body = "\n\n".join(buf).strip()
        if body:
            prefix = f"{heading}\n" if heading else ""
            chunks.append({"text": (prefix + body).strip(), "source": source, "heading": heading})
        buf, buf_len = [], 0

    for para in re.split(r"\n\s*\n", text):
        p = para.strip()
        if not p:
            continue
        if p.lstrip().startswith("#"):          # a heading line: it becomes context
            flush()
            heading = p.lstrip("#").strip()
            continue
        if buf and buf_len + len(p) > max_chars:
            flush()
        buf.append(p)
        buf_len += len(p) + 2
    flush()
    return chunks


def iter_doc_files():
    """The docs to index: README, CLAUDE.md, and everything under docs/."""
    for rel in ("README.md", "CLAUDE.md"):
        f = PROJECT_ROOT / rel
        if f.exists():
            yield f
    docs_dir = PROJECT_ROOT / "docs"
    if docs_dir.exists():
        yield from sorted(docs_dir.rglob("*.md"))


def _collection(chroma_dir, create=False):
    #chromadb imported here, not at module top, so importing rag (and the
    #assistant) works even where chromadb isn't installed; only building/
    #retrieving needs it.
    import chromadb
    client = chromadb.PersistentClient(path=str(chroma_dir))
    if create:
        try:
            client.delete_collection(COLLECTION)   # rebuild fresh: no stale chunks
        except Exception:
            pass
        #cosine space: compare direction (meaning), not vector length
        return client.create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})
    return client.get_collection(COLLECTION)


def build_index(embedder=None, chroma_dir=CHROMA_DIR, files=None, verbose=True):
    """Chunk the docs, embed them, and store the vectors in ChromaDB."""
    embedder = embedder or ollama_embed
    files = list(files) if files is not None else list(iter_doc_files())

    ids, documents, metadatas = [], [], []
    for f in files:
        fp = Path(f)
        try:
            rel = fp.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            rel = fp.name   # a file outside the project root (e.g. a test temp dir)
        for i, ch in enumerate(chunk_markdown(Path(f).read_text(encoding="utf-8"), rel)):
            ids.append(f"{rel}#{i}")
            documents.append(ch["text"])
            metadatas.append({"source": ch["source"], "heading": ch["heading"]})

    if not documents:
        if verbose:
            print("[rag] no documents found to index")
        return 0

    coll = _collection(chroma_dir, create=True)
    coll.add(ids=ids, documents=documents, embeddings=embedder(documents), metadatas=metadatas)
    if verbose:
        n_docs = len({m["source"] for m in metadatas})
        print(f"[rag] indexed {len(documents)} chunks from {n_docs} docs -> {chroma_dir}")
    return len(documents)


def retrieve(query, k=4, embedder=None, chroma_dir=CHROMA_DIR):
    """Return the k passages whose meaning is closest to the question:
    {"passages": [{"text", "source"}, ...]}, or an error note if the index
    isn't there (so the assistant can tell the user to build it)."""
    embedder = embedder or ollama_embed
    try:
        coll = _collection(chroma_dir, create=False)
    except ImportError:
        return {"passages": [], "error": "chromadb not installed (pip install chromadb)"}
    except Exception:
        return {"passages": [], "error": "no doc index yet; run: python -m iot_lab.rag build"}

    res = coll.query(query_embeddings=embedder([query]), n_results=k)
    documents = (res.get("documents") or [[]])[0]
    metadatas = (res.get("metadatas") or [[]])[0]
    return {"passages": [{"text": d, "source": (m or {}).get("source", "?")}
                         for d, m in zip(documents, metadatas)]}


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "build":
        build_index()
    elif len(sys.argv) >= 2:
        out = retrieve(" ".join(sys.argv[1:]))
        if out.get("error"):
            print(out["error"])
        for p in out["passages"]:
            print(f"--- {p['source']} ---\n{p['text']}\n")
    else:
        print("usage: python -m iot_lab.rag build | python -m iot_lab.rag <query>")
