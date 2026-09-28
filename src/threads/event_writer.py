import queue
import sqlite3

from sqlite_handler import SQLiteHandler

#Sentinel ("poison pill"): once the writer dequeues this, it closes and returns
STOP = None


class EventWriter:
    """
    Consumer thread: drains detection events from the queue into SQLite,
    one insert per event (see docs/adr/0001-event-pipeline.md).

    Shutdown does NOT use stop_event. Call stop() only after every producer
    has finished; the queue is FIFO, so every event put before the sentinel
    is written before the thread exits.
    """

    def __init__(self, event_queue: queue.Queue, db_path: str):
        self.queue = event_queue
        self.db_path = db_path

    def task_writer(self):
        #Open the DB here, not in __init__: sqlite3 connections belong to
        #the thread that created them, and __init__ runs on the main thread
        db = SQLiteHandler(self.db_path)

        try:
            while True:
                #Blocking get is fine: the sentinel is guaranteed to wake us
                event = self.queue.get()
                if event is STOP:
                    break

                #A single bad event shouldn't kill the writer thread
                try:
                    db.insert_event(event)
                except (KeyError, sqlite3.Error) as e:
                    print(f"[DB] dropped event {event}: {e}")
        finally:
            db.close()

    def stop(self):
        self.queue.put(STOP)
