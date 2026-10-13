import json
import math
import threading
import time

from sqlite_handler import SQLiteHandler
from aws_publisher import AWSPublisher


class Stream:
    """
    One store-and-forward stream: a SQLite table drained into the cloud under
    its own cursor (docs/adr/0008-sensor-readings.md). Adding a sensor stream is
    a new Stream in the list below, not a change to the forward logic.
      consumer   : its row in consumer_offsets (its cursor name)
      fetch_name : SQLiteHandler method that returns rows after an id
      to_payload : builds the DynamoDB item (incl. idempotent event_key)
    """

    def __init__(self, consumer: str, fetch_name: str, to_payload):
        self.consumer = consumer
        self.fetch_name = fetch_name
        self.to_payload = to_payload
        #Per-stream store-and-forward state (set once the DB is open)
        self.cursor = 0
        self.in_flight_end = None


class AWSForwarder:
    """
    Store-and-forward consumer (docs/adr/0002-store-and-forward.md), now driving
    one or more streams (ADR 0008). SQLite is the source of truth. For each
    stream this thread reads rows the cloud hasn't acknowledged yet, publishes
    them in batches, and advances that stream's cursor only once the WHOLE batch
    is acknowledged. Delivery is at-least-once; event_key makes re-sends
    idempotent on the cloud side.

    Streams are drained one at a time: the publisher's ack-wait is global, so we
    never have two streams' batches in flight at once.
    """

    def __init__(self, db_path: str, publisher: AWSPublisher, streams=None,
                 batch_size=50, ack_timeout_s=5.0, poll_s=1.0, shutdown_timeout_s=5.0):
        self.db_path = db_path
        self.publisher = publisher
        self.batch_size = batch_size
        self.ack_timeout_s = ack_timeout_s
        self.poll_s = poll_s
        self.shutdown_timeout_s = shutdown_timeout_s

        #Default streams: detections (consumer name unchanged, so its existing
        #cursor keeps resuming) + sensor readings. A stream with no rows is a
        #no-op, so including readings costs nothing when no sensor is running.
        self.streams = streams if streams is not None else [
            Stream("aws_iot",          "fetch_events_after",   self.detection_payload),
            Stream("aws_iot_readings", "fetch_readings_after", self.reading_payload),
        ]

        #Set by the writer(s) after each insert, so new rows go out immediately
        self.wakeup = threading.Event()
        self.stopping = threading.Event()

    def task_forwarder(self):
        #Own connection, opened in this thread (see SQLiteHandler docstring)
        try:
            db = SQLiteHandler(self.db_path)
        except Exception as e:
            print(f"[AWS] forwarder could not open DB, cloud upload disabled: {e}")
            return

        try:
            for s in self.streams:
                s.cursor = db.get_offset(s.consumer)
                print(f"[AWS] forwarder '{s.consumer}' resuming after event id {s.cursor}")

            while not self.stopping.is_set():
                self._forward_all(db)

                #Sleep until a new row, stop(), or the poll interval. The poll
                #flushes the backlog after a reconnect, when no new row may
                #arrive to wake us.
                self.wakeup.wait(self.poll_s)
                self.wakeup.clear()

            #Final bounded drain: main stops us only after the writers finished,
            #so every row is in SQLite. Whatever doesn't make it is sent next run.
            self._forward_all(db, deadline=time.monotonic() + self.shutdown_timeout_s)
        finally:
            db.close()

    def _forward_all(self, db, deadline=math.inf):
        #Drain each stream in turn (one stream in flight at a time)
        for s in self.streams:
            self._forward_stream(db, s, deadline)

    def _forward_stream(self, db, stream, deadline):
        #Deliver this stream's backlog batch by batch; advance its cursor only
        #when a whole batch is acked.
        fetch = getattr(db, stream.fetch_name)

        while self.publisher.connected and time.monotonic() < deadline:

            #In normal operation, hand over to the final drain as soon as stop() is called
            if deadline == math.inf and self.stopping.is_set():
                break

            if stream.in_flight_end is None:
                rows = fetch(stream.cursor, self.batch_size)
                if not rows:
                    break
                stream.in_flight_end = self._publish_batch(rows, stream.to_payload)
                if stream.in_flight_end is None:
                    break   #first publish rejected; retry on the next wakeup

            ack_timeout = min(self.ack_timeout_s, deadline - time.monotonic())
            if not self.publisher.wait_for_acks(ack_timeout):
                break   #offline or slow; paho keeps retrying the in-flight batch

            #Whole batch acknowledged: only now is it safe to move the cursor
            stream.cursor = stream.in_flight_end
            stream.in_flight_end = None
            db.set_offset(stream.consumer, stream.cursor)

    def _publish_batch(self, rows, to_payload):
        #Returns the id of the last row in the accepted prefix of the batch.
        #Stop at the first rejection so the cursor can never skip over a row.
        last_accepted = None
        for row in rows:
            if not self.publisher.publish(to_payload(row)):
                break
            last_accepted = row["id"]
        return last_accepted

    # ---------- payload builders (one per stream) ----------

    @staticmethod
    def detection_payload(row: dict) -> dict:
        """
        Detection item. event_key = "<ts>#<run_id>#<track_id>#<event_type>",
        built only from stored columns so a re-sent event overwrites itself.
          partition key: sensor_id   sort key: event_key
        """
        payload = {k: v for k, v in row.items() if k != "id"}
        payload["event_key"] = f"{row['ts']}#{row['run_id']}#{row['track_id']}#{row['event_type']}"
        return payload

    @staticmethod
    def reading_payload(row: dict) -> dict:
        """
        Reading item (ADR 0008). The measured values are stored packed in the
        `payload` JSON column; unpack them to top-level so the DynamoDBv2 rule
        maps each to its own attribute, matching detections' flat shape.
        event_key = "<ts>#<sensor_id>#<sensor_type>" (idempotent re-send key).
        """
        values = json.loads(row["payload"])
        return {
            "sensor_id": row["sensor_id"],
            "ts": row["ts"],
            "sensor_type": row["sensor_type"],
            "event_key": f"{row['ts']}#{row['sensor_id']}#{row['sensor_type']}",
            **values,
        }

    def stop(self):
        self.stopping.set()
        self.wakeup.set()
