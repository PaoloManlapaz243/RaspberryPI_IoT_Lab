"""SQLiteHandler storage: the tracks view and consumer cursors (ADRs 0001, 0002)."""
import pytest

from iot_lab.sqlite_handler import SQLiteHandler


def event(track_id, event_type, ts, class_name="person", run_id="run-1"):
    return {"ts": ts, "sensor_id": "S1", "run_id": run_id, "track_id": track_id,
            "event_type": event_type, "class_name": class_name, "confidence": 0.9,
            "x1": 0, "y1": 0, "x2": 1, "y2": 1}

@pytest.fixture
def db(tmp_path):
    handler = SQLiteHandler(str(tmp_path / "events.db"))
    yield handler
    handler.close()

def visits(db):
    rows = db.conn.execute(
        "SELECT track_id, entered_at, exited_at FROM tracks ORDER BY entered_at").fetchall()
    return [tuple(row) for row in rows]

def test_open_visit_has_no_exit(db):
    db.insert_event(event(1, "enter", "t1"))
    assert visits(db) == [(1, "t1", None)]

def test_revived_track_id_pairs_each_enter_with_next_exit(db):
    #Regression: the tracker revived an ID after its exit, and the old join
    #paired every enter with every exit (N x M rows)
    for ts, kind in [("t1", "enter"), ("t2", "exit"), ("t3", "enter"), ("t4", "exit")]:
        db.insert_event(event(7, kind, ts))
    assert visits(db) == [(7, "t1", "t2"), (7, "t3", "t4")]

def test_same_track_id_in_another_run_is_a_different_visit(db):
    db.insert_event(event(1, "enter", "t1", run_id="run-1"))
    db.insert_event(event(1, "exit", "t2", run_id="run-2"))
    assert visits(db) == [(1, "t1", None)]

def test_offset_starts_at_zero_and_round_trips(db):
    assert db.get_offset("aws_iot") == 0
    db.set_offset("aws_iot", 5)
    db.set_offset("aws_iot", 9)
    assert db.get_offset("aws_iot") == 9
    assert db.get_offset("other") == 0

def test_fetch_events_after_is_oldest_first_and_limited(db):
    for i in range(1, 6):
        db.insert_event(event(i, "enter", f"t{i}"))
    rows = db.fetch_events_after(2, limit=2)
    assert [row["id"] for row in rows] == [3, 4]
