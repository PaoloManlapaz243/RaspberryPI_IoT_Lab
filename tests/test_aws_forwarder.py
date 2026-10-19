"""to_payload(): the DynamoDB item and its idempotent sort key (ADR 0002)."""
from iot_lab.threads.aws_forwarder import AWSForwarder


ROW = {"id": 42, "ts": "2026-10-12T10:00:00.000Z", "sensor_id": "S1", "run_id": "abc",
       "track_id": 3, "event_type": "enter", "class_name": "dog", "confidence": 0.8,
       "x1": 0, "y1": 0, "x2": 1, "y2": 1}

def test_event_key_is_built_from_stored_columns():
    payload = AWSForwarder.to_payload(ROW)
    assert payload["event_key"] == "2026-10-12T10:00:00.000Z#abc#3#enter"

def test_local_id_is_not_uploaded():
    assert "id" not in AWSForwarder.to_payload(ROW)

def test_resent_event_gets_the_same_key():
    #At-least-once delivery: a re-send must overwrite, not duplicate
    assert AWSForwarder.to_payload(ROW) == AWSForwarder.to_payload(dict(ROW))
