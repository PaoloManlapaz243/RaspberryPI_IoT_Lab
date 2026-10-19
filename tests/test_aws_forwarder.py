"""AWSForwarder: payload builders (ADRs 0002, 0009) and per-stream cursors."""
import json

import pytest

from iot_lab.sqlite_handler import SQLiteHandler
from iot_lab.threads.aws_forwarder import AWSForwarder


ROW = {"id": 42, "ts": "2026-10-12T10:00:00.000Z", "sensor_id": "S1", "run_id": "abc",
       "track_id": 3, "event_type": "enter", "class_name": "dog", "confidence": 0.8,
       "x1": 0, "y1": 0, "x2": 1, "y2": 1}

READING_ROW = {"id": 7, "ts": "2026-10-12T10:00:03.000Z", "sensor_id": "T1",
               "sensor_type": "dht11", "payload": '{"temperature_c": 22.0, "humidity": 41}'}

def test_event_key_is_built_from_stored_columns():
    payload = AWSForwarder.detection_payload(ROW)
    assert payload["event_key"] == "2026-10-12T10:00:00.000Z#abc#3#enter"

def test_local_id_is_not_uploaded():
    assert "id" not in AWSForwarder.detection_payload(ROW)

def test_resent_event_gets_the_same_key():
    #At-least-once delivery: a re-send must overwrite, not duplicate
    assert AWSForwarder.detection_payload(ROW) == AWSForwarder.detection_payload(dict(ROW))

def test_reading_payload_is_flat_with_its_own_key():
    assert AWSForwarder.reading_payload(READING_ROW) == {
        "sensor_id": "T1", "ts": "2026-10-12T10:00:03.000Z", "sensor_type": "dht11",
        "event_key": "2026-10-12T10:00:03.000Z#T1#dht11",
        "temperature_c": 22.0, "humidity": 41,
    }


class FakePublisher:
    """Always connected; acknowledges everything. Records what was sent."""
    connected = True
    def __init__(self):
        self.sent = []
    def publish(self, payload):
        self.sent.append(payload)
        return True
    def wait_for_acks(self, timeout):
        return True

@pytest.fixture
def db(tmp_path):
    handler = SQLiteHandler(str(tmp_path / "events.db"))
    yield handler
    handler.close()

def test_each_stream_uploads_its_table_and_moves_its_own_cursor(db):
    db.insert_event({k: v for k, v in ROW.items() if k != "id"})
    for i in range(3):
        db.insert_reading({"ts": f"t{i}", "sensor_id": "T1", "sensor_type": "dht11",
                           "payload": {"temperature_c": 20.0 + i, "humidity": 40}})
    publisher = FakePublisher()
    forwarder = AWSForwarder(db_path=None, publisher=publisher, batch_size=2)
    for stream in forwarder.streams:
        stream.cursor = db.get_offset(stream.consumer)

    forwarder._forward_all(db)

    assert db.get_offset("aws_iot") == 1
    assert db.get_offset("aws_iot_readings") == 3
    assert [p.get("sensor_type") for p in publisher.sent] == [None, "dht11", "dht11", "dht11"]

    #Nothing new: a second pass sends nothing
    forwarder._forward_all(db)
    assert len(publisher.sent) == 4
