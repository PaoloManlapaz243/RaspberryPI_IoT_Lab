"""route() control flow (ADR 0007): guard question -> model -> guard answer.
The model is replaced by canned replies, so these test our code, not the model."""
import json

import pytest

from iot_lab import detection_assistant as da


@pytest.fixture
def model(monkeypatch):
    """Queue canned router replies; records each call so tests can count them."""
    replies, calls = [], []
    def fake_ollama(messages, schema=None, temperature=0.0):
        calls.append(messages)
        return json.dumps(replies.pop(0))
    monkeypatch.setattr(da, "_ollama", fake_ollama)
    monkeypatch.setattr(da, "USE_GUARD", True)
    return replies, calls

def test_guarded_question_makes_no_model_call(model):
    replies, calls = model
    assert da.route("how many people came by since 9am?", verbose=False)["function"] == "unclear"
    assert calls == []

def test_valid_reply_is_returned(model):
    replies, calls = model
    replies.append({"function": "count", "args": {"label": "person", "since_minutes": 60}})
    call = da.route("how many people came by in the last hour?", verbose=False)
    assert call == {"function": "count", "args": {"label": "person", "since_minutes": 60}}

def test_second_label_in_question_declines(model):
    #The question guard can't read "people"; the model maps it, and the answer
    #guard sees "dogs" was dropped
    replies, calls = model
    replies.append({"function": "count", "args": {"label": "person", "since_minutes": None}})
    assert da.route("how many people and dogs came by?", verbose=False)["function"] == "unclear"

def test_invalid_reply_gets_one_retry_with_the_reason(model):
    replies, calls = model
    replies += [{"function": "last_seen", "args": {"label": None}},
                {"function": "last_seen", "args": {"label": "dog"}}]
    assert da.route("when did you last see a dog?", verbose=False)["args"] == {"label": "dog"}
    assert len(calls) == 2
    assert "requires label" in calls[1][-1]["content"]

def test_two_invalid_replies_give_up(model):
    replies, calls = model
    replies += [{"function": "last_seen", "args": {"label": None}}] * 2
    assert da.route("when did you last see it?", verbose=False)["function"] == "invalid"
    assert len(calls) == 2
