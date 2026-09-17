import threading
import queue
import time
from ultralytics import YOLO

from threads.camera import CameraHandler

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

    def task_inference(self):

        while not self.stopped.is_set():
            if not self.camera_frame_ready.is_set():
                self.camera_frame_ready.wait()
                continue

            self.inference_results = self.model(
                self.camera.get_frame(),
                imgsz = 320,
                conf = self.conf_threshold,
                device = 'cpu',
                verbose = False
            )

            #DECIDE IF I WANT TO DRAW THE RESULTS ONTO THE FRAME HERE
            self.inference_plotted = self.inference_results[0].plot()

            self.inference_frame_ready.set()

            #Write the detections to the queue
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
            detections = []

            # for result in self.inference_results:
            #     for box in result.boxes:
            #         detections.append(
            #             {
            #                 "label": self.model.names[int(box.cls)],
            #                 "confidence": round(float(box.conf), 2),
            #             }
            #         )

            # Append to memory queue if objects are found
            if detections:
                event = {"timestamp": timestamp, "detections": detections}
                self.queue.append(event)


    def getInferenceResults(self):
        return self.inference_results

    def getInferencePlot(self):
        return self.inference_plotted

