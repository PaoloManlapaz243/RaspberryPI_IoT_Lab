"""answer() for the docs intent (ADR 0010): routed to rag.retrieve(), phrased
only from the retrieved passages. Router, retriever and phraser are all
replaced, so this tests our control flow, not the model or ChromaDB."""
import json

import pytest

from iot_lab import detection_assistant as da


@pytest.fixture
def docs_route(monkeypatch):
    #Router always says docs; phrasing calls are recorded instead of sent
    phrase_calls = []
    def fake_ollama(messages, schema=None, temperature=0.0):
        if schema is not None:
            return json.dumps({"function": "docs", "args": {}})
        phrase_calls.append(messages)
        return "Keep it out of direct sun [docs/x.md]."
    monkeypatch.setattr(da, "_ollama", fake_ollama)
    return phrase_calls

def test_docs_question_is_phrased_from_retrieved_passages(docs_route, monkeypatch):
    queries = []
    def fake_retrieve(q):
        queries.append(q)
        return {"passages": [{"text": "Keep the sensor out of direct sun.", "source": "docs/x.md"}]}
    monkeypatch.setattr(da.rag, "retrieve", fake_retrieve)

    reply = da.answer("where should the temperature sensor go?")
    assert reply == "Keep it out of direct sun [docs/x.md]."
    assert queries == ["where should the temperature sensor go?"]   #the raw question
    prompt = docs_route[0][-1]["content"]
    assert "[docs/x.md]" in prompt and "direct sun" in prompt        #passage + source given

def test_no_index_declines_without_a_model_call(docs_route, monkeypatch):
    monkeypatch.setattr(da.rag, "retrieve",
                        lambda q: {"passages": [], "error": "no doc index yet"})
    reply = da.answer("how does store-and-forward work?")
    assert "couldn't find" in reply and "no doc index yet" in reply
    assert docs_route == []                                           #nothing phrased

def test_docs_takes_no_args():
    assert da.validate_call({"function": "docs", "args": {}}) == {"function": "docs", "args": {}}
    with pytest.raises(da.InvalidCall):
        da.validate_call({"function": "docs", "args": {"label": "person"}})
