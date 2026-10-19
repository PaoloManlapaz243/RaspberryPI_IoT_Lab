"""DHTSensor: the reading loop, with a fake device instead of the hardware."""
import json
import threading

from iot_lab.sqlite_handler import SQLiteHandler
from iot_lab.threads.dht_sensor import DHTSensor, make_reading


class FakeDHT:
    """Replays outcomes, one per read: (temperature_c, humidity) or an exception.
    Sets stop_event after the last one so the loop ends."""
    def __init__(self, outcomes, stop_event):
        self.outcomes = list(outcomes)
        self.stop_event = stop_event
        self.exited = False
        self._humidity = None

    @property
    def temperature(self):
        outcome = self.outcomes.pop(0)
        if not self.outcomes:
            self.stop_event.set()
        if isinstance(outcome, Exception):
            raise outcome
        temperature_c, self._humidity = outcome
        return temperature_c

    @property
    def humidity(self):
        return self._humidity

    def exit(self):
        self.exited = True

def run_sensor(tmp_path, outcomes):
    stop, inserted = threading.Event(), threading.Event()
    device = FakeDHT(outcomes, stop)
    sensor = DHTSensor(stop, str(tmp_path / "events.db"), read_every_s=0,
                       inserted=inserted, device=device)
    sensor.task_sensor()
    db = SQLiteHandler(str(tmp_path / "events.db"))
    rows = db.fetch_readings_after(0, limit=10)
    db.close()
    return rows, device, inserted

def test_make_reading_stores_measured_values_only():
    reading = make_reading("T1", 22.0, 41)
    assert reading["payload"] == {"temperature_c": 22.0, "humidity": 41}
    assert (reading["sensor_id"], reading["sensor_type"]) == ("T1", "dht11")

def test_failed_reads_are_skipped_and_good_ones_stored(tmp_path):
    rows, device, inserted = run_sensor(tmp_path, [
        RuntimeError("Checksum did not validate"),   #normal DHT11 miss
        (None, None),                                 #no reading
        (22.0, 41),
    ])
    assert [json.loads(row["payload"]) for row in rows] == [{"temperature_c": 22.0, "humidity": 41}]
    assert inserted.is_set()
    assert device.exited                              #hardware released on stop

def test_no_good_reads_store_nothing(tmp_path):
    rows, device, inserted = run_sensor(tmp_path, [RuntimeError("timeout")])
    assert rows == []
    assert not inserted.is_set()
