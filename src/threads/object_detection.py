import threading
import queue
import time
import numpy as np
from ultralytics import YOLO

from threads.camera import CameraHandler

from datetime import datetime

class InferenceHandler():
    def __init__(self, camera_event: threading.Event, detected_event: threading.Event, stopped_event: threading.Event, queue: queue.Queue, model: str, camera: CameraHandler, conf_threshold = 0.3):

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

        #Tracked Objects
        self.tracked_objects = {}

    def task_inference(self):

        while not self.stopped.is_set():
            if not self.camera_frame_ready.is_set():
                self.camera_frame_ready.wait()
                continue

            # self.inference_results = self.model(
            #     self.camera.get_frame(),
            #     imgsz = 320,
            #     conf = self.conf_threshold,
            #     device = 'cpu',
            #     verbose = False
            # )
            
            frame = self.camera.get_frame()
            if frame is None:
                continue

            self.camera_frame_ready.clear()

            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


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

            # Check if any objects were detected and tracked
            result_boxes = self.inference_results[0].boxes
            if result_boxes is None or result_boxes.id is None:
                continue

            result_boxes = result_boxes.cpu().numpy()
            boxes = np.asarray(result_boxes.xyxy)
            track_ids = np.asarray(result_boxes.id).astype(int)
            clss = np.asarray(result_boxes.cls).astype(int)
            confs = np.asarray(result_boxes.conf)
            names = self.model.names

            # 5. Process tracking states
            for box, track_id, cls, conf in zip(boxes, track_ids, clss, confs):
                class_name = names[cls]

                # If this is a new object ID, log the initial detection
                if track_id not in self.tracked_objects:
                    self.tracked_objects[track_id] = {
                        "class": class_name,
                        "first_seen": current_time,
                        "last_seen": current_time
                    }

                    #Add to queue
                    self.queue.put_nowait({"timestamp": current_time, "class": class_name, "confidence": conf, "box": box})

                    print(f"[NEW] ID {track_id} ({class_name}) detected at {current_time} with confidence {conf}")
                else:
                    # Update the last seen timestamp for existing object
                    self.tracked_objects[track_id]["last_seen"] = current_time


    def getInferenceResults(self):
        return self.inference_results

    def getInferencePlot(self):
        return self.inference_plotted

