import math
import threading
import time

from sqlite_handler import SQLiteHandler
from aws_publisher import AWSPublisher

#This consumer's name in the consumer_offsets table
CONSUMER = "aws_iot"


class AWSForwarder:
    """
    Store-and-forward consumer (docs/adr/0002-store-and-forward.md).

    SQLite is the source of truth. This thread reads events the cloud hasn't
    acknowledged yet, publishes them in batches, and advances its cursor in
    consumer_offsets only once the WHOLE batch is acknowledged. Anything not
    acknowledged (offline, crash, power loss) is re-sent from SQLite later, so
    delivery is at-least-once: the cloud side must tolerate duplicates, which
    is what event_key is for.
    """

    def __init__(self, db_path: str, publisher: AWSPublisher, batch_size = 50,
                 ack_timeout_s = 5.0, poll_s = 1.0, shutdown_timeout_s = 5.0):
        self.db_path = db_path
        self.publisher = publisher
        self.batch_size = batch_size
        self.ack_timeout_s = ack_timeout_s
        self.poll_s = poll_s
        self.shutdown_timeout_s = shutdown_timeout_s

        #Set by EventWriter after each insert, so new events go out immediately
        self.wakeup = threading.Event()
        self.stopping = threading.Event()

        #Last events.id of the batch handed to paho but not yet acknowledged.
        #paho re-sends it after a reconnect, so we wait for it rather than
        #re-sending from SQLite (fewer duplicates within one run).
        self._in_flight_end = None

    def task_forwarder(self):
        #Own connection, opened in this thread (see SQLiteHandler docstring)
        try:
            db = SQLiteHandler(self.db_path)
        except Exception as e:
            print(f"[AWS] forwarder could not open DB, cloud upload disabled: {e}")
            return

        try:
            cursor = db.get_offset(CONSUMER)
            print(f"[AWS] forwarder resuming after event id {cursor}")

            while not self.stopping.is_set():
                cursor = self._forward(db, cursor)

                #Sleep until a new event, stop(), or the poll interval. The poll
                #is what flushes the backlog after a reconnect, when no new
                #event may arrive to wake us.
                self.wakeup.wait(self.poll_s)
                self.wakeup.clear()

            #Final bounded drain: main stops us only after the writer finished,
            #so every event is in SQLite. Whatever doesn't make it is sent next run.
            cursor = self._forward(db, cursor, deadline = time.monotonic() + self.shutdown_timeout_s)
        finally:
            db.close()

    def _forward(self, db, cursor, deadline = math.inf):
        #Deliver the backlog batch by batch; returns the (possibly advanced) cursor
        while self.publisher.connected and time.monotonic() < deadline:

            #In normal operation, hand over to the final drain as soon as stop() is called
            if deadline == math.inf and self.stopping.is_set():
                break

            if self._in_flight_end is None:
                rows = db.fetch_events_after(cursor, self.batch_size)
                if not rows:
                    break
                self._in_flight_end = self._publish_batch(rows)
                if self._in_flight_end is None:
                    break   #first publish rejected; retry on the next wakeup

            ack_timeout = min(self.ack_timeout_s, deadline - time.monotonic())
            if not self.publisher.wait_for_acks(ack_timeout):
                break   #offline or slow; paho keeps retrying the in-flight batch

            #Whole batch acknowledged: only now is it safe to move the cursor
            cursor = self._in_flight_end
            self._in_flight_end = None
            db.set_offset(CONSUMER, cursor)

        return cursor

    def _publish_batch(self, rows):
        #Returns the id of the last row in the accepted prefix of the batch.
        #Stop at the first rejection so the cursor can never skip over a row.
        last_accepted = None
        for row in rows:
            if not self.publisher.publish(self.to_payload(row)):
                break
            last_accepted = row["id"]
        return last_accepted

    @staticmethod
    def to_payload(row: dict) -> dict:
        """
        The DynamoDB item (the IoT Rule writes each top-level field as an attribute).
          partition key: sensor_id
          sort key:      event_key = "<ts>#<run_id>#<track_id>#<event_type>"
        event_key is built only from stored columns, so a re-sent event gets the
        SAME key and overwrites itself instead of creating a duplicate item. It
        starts with ts, so items sort by time within a sensor.
        """
        payload = {k: v for k, v in row.items() if k != "id"}
        payload["event_key"] = f"{row['ts']}#{row['run_id']}#{row['track_id']}#{row['event_type']}"
        return payload

    def stop(self):
        self.stopping.set()
        self.wakeup.set()
