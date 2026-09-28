import threading
import queue
import time
import numpy as np
from ultralytics import YOLO

from threads.camera import CameraHandler

from datetime import datetime, timezone

#Seconds a track can go unseen before we emit its exit event.
#Too short: tracker flicker creates false exit/enter pairs. Too long: late exits.
EXIT_TIMEOUT_S = 2.0


def utc_timestamp():
    #ISO 8601 UTC with milliseconds, e.g. 2026-09-28T04:38:15.123Z
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class InferenceHandler():
    def __init__(self, camera_event: threading.Event, detected_event: threading.Event, stopped_event: threading.Event, queue: queue.Queue, model: str, camera: CameraHandler, conf_threshold = 0.3, sensor_id = "S1", run_id = None, exit_timeout_s = EXIT_TIMEOUT_S):

        #State Management
        self.camera_frame_ready = camera_event
        self.inference_frame_ready = detected_event
        self.stopped = stopped_event

        #Data structure for event logging
        self.queue = queue

        #Model initialization
        self.model = YOLO(model, task = 'detect')

        #Frame Save
        self.inference_frame = None
        self.inference_plotted = None

        #Camera Handler
        self.camera = camera

        #Confidence Threshold
        self.conf_threshold = conf_threshold

        #Event identity: track IDs restart at 1 every run, so (run_id, track_id)
        #is what uniquely identifies a track in the database
        self.sensor_id = sensor_id
        self.run_id = run_id if run_id is not None else utc_timestamp()
        self.exit_timeout_s = exit_timeout_s

        #Tracked Objects: track_id -> latest state, removed when it exits
        self.tracked_objects = {}

    def task_inference(self):

        while not self.stopped.is_set():
            if not self.camera_frame_ready.is_set():
                self.camera_frame_ready.wait(timeout=0.1)
                continue

            # self.inference_results = self.model(
            #     self.camera.get_frame(),
            #     imgsz = 320,
            #     conf = self.conf_threshold,
            #     device = 'cpu',
            #     verbose = False
            # )

            self.camera_frame_ready.clear()            
            frame = self.camera.get_frame()
            if frame is None:
                continue


            self.inference_results = self.model.track(
                frame,
                persist=True,
                imgsz=320,
                conf=self.conf_threshold,
                device='cpu',
                verbose=False
            )

            #DECIDE IF I WANT TO DRAW THE RESULTS ONTO THE FRAME HERE
            self.inference_plotted = self.inference_results[0].plot()
            
            self.inference_frame_ready.set()

            self._process_detections(self.inference_results[0].boxes)

            #Runs every frame, including frames with no detections
            self._sweep_exits()

        #Shutting down: close out every track still in view so the DB has no
        #dangling enters. main must put the writer's sentinel AFTER this returns.
        for track_id in list(self.tracked_objects):
            self._emit_exit(track_id)

    def _process_detections(self, result_boxes):
        # Check if any objects were detected and tracked
        if result_boxes is None or result_boxes.id is None:
            return

        result_boxes = result_boxes.cpu().numpy()
        boxes = np.asarray(result_boxes.xyxy)
        track_ids = np.asarray(result_boxes.id).astype(int)
        clss = np.asarray(result_boxes.cls).astype(int)
        confs = np.asarray(result_boxes.conf)
        names = self.model.names

        now_ts = utc_timestamp()
        now_mono = time.monotonic()

        for box, track_id, cls, conf in zip(boxes, track_ids, clss, confs):
            #Convert numpy types to plain Python so events are JSON/SQLite safe
            track_id = int(track_id)
            state = {
                "class_name": names[int(cls)],
                "confidence": round(float(conf), 3),
                "box": [round(float(v), 1) for v in box],
                "last_seen_ts": now_ts,        #wall clock, stored in the DB
                "last_seen_mono": now_mono,    #monotonic, only for the timeout
            }

            # If this is a new object ID, log the initial detection
            if track_id not in self.tracked_objects:
                #Keep the class from first sighting; the per-frame class can flicker
                self.tracked_objects[track_id] = state
                self._emit(track_id, "enter", state, now_ts)
            else:
                state["class_name"] = self.tracked_objects[track_id]["class_name"]
                self.tracked_objects[track_id] = state

    def _sweep_exits(self):
        now_mono = time.monotonic()
        expired = [
            track_id for track_id, state in self.tracked_objects.items()
            if now_mono - state["last_seen_mono"] > self.exit_timeout_s
        ]
        for track_id in expired:
            self._emit_exit(track_id)

    def _emit_exit(self, track_id):
        #Exit is stamped with the LAST SIGHTING, not when the timeout fired,
        #so exited_at reflects when the object actually left
        state = self.tracked_objects.pop(track_id)
        self._emit(track_id, "exit", state, state["last_seen_ts"])

    def _emit(self, track_id, event_type, state, ts):
        x1, y1, x2, y2 = state["box"]
        event = {
            "ts": ts,
            "sensor_id": self.sensor_id,
            "run_id": self.run_id,
            "track_id": track_id,
            "event_type": event_type,
            "class_name": state["class_name"],
            "confidence": state["confidence"],
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
        }
        self.queue.put_nowait(event)
        print(f"[{event_type.upper()}] ID {track_id} ({state['class_name']}) at {ts} conf {state['confidence']}")


    def getInferenceResults(self):
        return self.inference_results

    def getInferencePlot(self):
        return self.inference_plotted

