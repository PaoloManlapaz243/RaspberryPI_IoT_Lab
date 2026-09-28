import queue
import threading
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

    def __init__(self, event_queue: queue.Queue, db_path: str, inserted: threading.Event = None):
        self.queue = event_queue
        self.db_path = db_path

        #Optional: set after every insert to wake downstream consumers (e.g. the
        #AWS forwarder). The writer doesn't know or care who is listening.
        self.inserted = inserted

        #Set once the DB is open (or failed to open); see wait_until_ready()
        self.ready = threading.Event()
        self.error = None

    def task_writer(self):
        #Open the DB here, not in __init__: sqlite3 connections belong to
        #the thread that created them, and __init__ runs on the main thread
        try:
            db = SQLiteHandler(self.db_path)
        except Exception as e:
            self.error = e
            self.ready.set()
            return
        self.ready.set()

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
                    continue

                if self.inserted is not None:
                    self.inserted.set()
        finally:
            db.close()

    def wait_until_ready(self, timeout=5.0):
        #True if the DB opened successfully; call after starting the thread
        return self.ready.wait(timeout) and self.error is None

    def stop(self):
        self.queue.put(STOP)
