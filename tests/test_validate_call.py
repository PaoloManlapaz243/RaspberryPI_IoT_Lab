"""validate_call(): the "validate" layer of ADR 0004. Rejects rather than guesses."""
import pytest

from iot_lab.detection_assistant import InvalidCall, NOT_A_DETECTOR_LABEL, validate_call


def test_valid_call_is_returned_clean():
    call = {"function": "count", "args": {"label": "person", "since_minutes": 60}}
    assert validate_call(call) == call

def test_null_args_mean_not_given():
    call = {"function": "count", "args": {"label": None, "since_minutes": None}}
    assert validate_call(call) == {"function": "count", "args": {}}

def test_string_null_is_normalized():
    #Small models write the string "null" where the schema allows null
    call = {"function": "count", "args": {"label": "dog", "since_minutes": "null"}}
    assert validate_call(call) == {"function": "count", "args": {"label": "dog"}}

def test_label_is_canonicalized():
    call = {"function": "last_seen", "args": {"label": "Dogs"}}
    assert validate_call(call)["args"] == {"label": "dog"}

def test_not_a_detector_label_declines_without_retry():
    #A decline, not a format error: returns unclear instead of raising
    call = {"function": "count", "args": {"label": NOT_A_DETECTOR_LABEL}}
    assert validate_call(call)["function"] == "unclear"

@pytest.mark.parametrize("call, reason", [
    ("not a dict",                                                  "JSON object"),
    ({"function": "delete_everything", "args": {}},                 "unknown function"),
    ({"function": "count", "args": {"label": "vehicle"}},           "not a label"),
    ({"function": "last_seen", "args": {}},                         "requires label"),
    ({"function": "present_now", "args": {"label": "person"}},      "does not take"),
    ({"function": "count", "args": {"since_minutes": 0}},           "since_minutes"),
    ({"function": "count", "args": {"since_minutes": True}},        "since_minutes"),
    ({"function": "recent", "args": {"n": 51}},                     "n must be"),
])
def test_invalid_calls_are_rejected(call, reason):
    with pytest.raises(InvalidCall, match=reason):
        validate_call(call)
