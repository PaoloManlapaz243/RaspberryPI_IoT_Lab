"""The code guard (ADR 0005): declines unsupported questions before any model call."""
import pytest

from iot_lab.detection_assistant import canonical_label, labels_mentioned, unsupported_features


@pytest.mark.parametrize("question, why", [
    ("how many people came by since 9am?",     "a clock time"),
    ("did you see a dog this morning?",        "a part of the day"),
    ("how many cars yesterday?",               "a past day"),
    ("how long did the cat stay?",             "how long something stayed"),
    ("how many cats and dogs came by?",        "several object types at once"),
])
def test_unsupported_questions_are_flagged(question, why):
    assert why in unsupported_features(question)

@pytest.mark.parametrize("question", [
    "how many people came by in the last hour?",
    "is anyone there?",
    "when did you last see a dog?",
])
def test_supported_questions_pass(question):
    assert unsupported_features(question) == []

def test_labels_mentioned_includes_two_word_labels():
    assert labels_mentioned("did a cell phone and a cup show up?") == {"cell phone", "cup"}

@pytest.mark.parametrize("word, label", [
    ("cat", "cat"),
    ("Cats", "cat"),
    ("  DOG ", "dog"),
    ("buses", "bus"),
    ("vehicles", None),      #a category, not a detector label
    ("people", None),        #irregular plurals are the model's job (ADR 0006)
])
def test_canonical_label(word, label):
    assert canonical_label(word) == label
