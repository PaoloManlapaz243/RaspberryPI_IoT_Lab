"""rag.py: chunking + build/retrieve round-trip. A FAKE embedder replaces
Ollama so these test our code, not the model or the network. chromadb is
required; skipped if it isn't installed."""
import pytest

pytest.importorskip("chromadb")
from iot_lab import rag


def test_chunk_carries_heading_and_splits_paragraphs():
    text = "# Setup\n\nFirst paragraph about wiring.\n\nSecond paragraph about power."
    chunks = rag.chunk_markdown(text, "docs/x.md", max_chars=40)
    assert all(c["source"] == "docs/x.md" for c in chunks)
    assert all(c["text"].startswith("Setup") for c in chunks)   # heading prefixed
    assert len(chunks) == 2                                      # split at max_chars


#A deterministic bag-of-words embedder: similar wording -> similar vector, so
#nearest-neighbour returns the passage that shares the query's words.
def _fake_embedder(vocab):
    def embed(texts):
        out = []
        for t in texts:
            words = set(t.lower().split())
            out.append([1.0 if w in words else 0.0 for w in vocab])
        return out
    return embed


def test_build_and_retrieve_finds_the_right_doc(tmp_path):
    docs = {
        "wifi.md":   "# WiFi\n\nConnect the device to the hotspot network.",
        "sensor.md": "# Placement\n\nKeep the temperature sensor away from sunlight and heat.",
        "mqtt.md":   "# Messaging\n\nThe broker relays messages between devices.",
    }
    files = []
    for name, body in docs.items():
        f = tmp_path / name
        f.write_text(body)
        files.append(f)

    vocab = sorted({w.lower() for b in docs.values() for w in b.split()}
                   | {"put", "temperature", "where"})
    emb = _fake_embedder(vocab)

    # monkeypatch relative-path handling: tmp files aren't under PROJECT_ROOT,
    # so build_index uses str(f) as the source for absolute paths outside root
    n = rag.build_index(embedder=emb, chroma_dir=tmp_path / "chroma", files=files, verbose=False)
    assert n == 3

    out = rag.retrieve("where should I put the temperature sensor?",
                       k=1, embedder=emb, chroma_dir=tmp_path / "chroma")
    assert out["passages"], out
    assert "sunlight" in out["passages"][0]["text"]       # retrieved the placement doc
    assert out["passages"][0]["source"].endswith("sensor.md")


def test_retrieve_without_index_returns_error(tmp_path):
    out = rag.retrieve("anything", chroma_dir=tmp_path / "empty", embedder=_fake_embedder(["x"]))
    assert out["passages"] == [] and "error" in out
