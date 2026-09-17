import os
import time
import threading
import cv2
from ultralytics import YOLO
from tinydb import TinyDB
from aws_publisher import AWSPublisher

import queue
#frdaom dotenv impoasdasrt AWS_ENDPOINT

from threads.camera import CameraHandler
from threads.object_detection import InferenceHandler
from gui_handler import GUIHandler



#Rate Limiting DB Logging (write every 3 seconds)
LOG_INTERVAL_SEC = 5.0

#Set True for headless setup for greater performance
HEADLESS = False

class RasPiDeploy:
    def __init__(self, src=0, model_dir = "../models/yolo11n_ncnn_model", height = 640, width = 480, conf_thresh = 0.3):

        # Load the exported NCNN model directory
        #self.model = YOLO(model_dir, task = 'detect')

        #db logs
        #self.db = TinyDB("logs/camera_logs.json")
        #self.memory_queue = []
        #self.last_logged_frame_timestamp = time.time()

        #aws logging
        # self.cloud = AWSPublisher(
        #     #endpoint="XXXXXX-ats.iot.us-east-1.amazonaws.com",  # your IoT endpoint
        #     #endpoint = AWS_ENDPOINT,
        #     endpoint = "alqw25622p8x0-ats.iot.us-east-2.amazonaws.com",
        #     ca_path="certs/AmazonRootCA1.pem",
        #     cert_path="certs/detector-01.cert.pem",
        #     key_path="certs/detector-01.private.key",
        #     sensor_id="S1",
        #     #client_id="laptop-dev",   # give the Pi a DIFFERENT id later
        #     client_id = "detector-01"
        # )

        #Global data to pass
        self.conf_thresh = conf_thresh

        # Thread lock and stop
        self.lock = threading.Lock()

        # New Event-based locking
        self.stop_event = threading.Event()
        self.stop_event.clear()


        #Thread-safe datastructure
        self.queue = queue.Queue()
        
        #Threaded State Variables
        self.camera_frame_ready = threading.Event()
        self.inference_frame_ready = threading.Event()
        self.camera_frame_ready.clear()
        self.inference_frame_ready.clear()

        #Initialize Camera Handler
        self.camera = CameraHandler(
            self.camera_frame_ready, 
            self.stop_event, 
            src, 
            width, 
            height
        )

        #Initialize Inference Handler
        self.object_detect = InferenceHandler(
            self.camera_frame_ready,
            self.inference_frame_ready,
            self.stop_event,
            self.queue,
            model_dir,
            self.camera,
            conf_thresh
        )

        #Initialize GUI Handler
        self.gui = GUIHandler(
            self.inference_frame_ready,
            self.stop_event
        )



    # def task_inference(self):
    #     while not self.stopped:

    #         #obtain the mutex to check the primitive variables
    #         with self.lock:
    #             #read in the new camera frame, set a local flag to use outside lock
    #             self.perform_Inference = self.camera_frame_ready
    #             self.frame_to_inference = self.camera_frame

    #             #mark the current frame as processed
    #             self.camera_frame_ready = False
    #             self.camera_frame = None

    #         # sleep to avoid CPU thrasing
    #         if not self.perform_Inference:
    #             time.sleep(0.001)
    #             continue

    #         self.inference_results = self.model(self.frame_to_inference, imgsz = 320, conf = self.conf_thresh, device='cpu', verbose = False)
    #         self.inference_plotted = self.inference_results[0].plot()

    #         #if sufficient time since the last data log
    #         current_time = time.time()
    #         if current_time - self.last_logged_frame_timestamp >= LOG_INTERVAL_SEC:
    #             #append to the log
    #             timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(current_time))
    #             detections = []

    #             for result in self.inference_results:
    #                 for box in result.boxes:
    #                     detections.append(
    #                         {
    #                             "label": self.model.names[int(box.cls)],
    #                             "confidence": round(float(box.conf), 2),
    #                         }
    #                     )

    #             # Append to memory queue if objects are found
    #             if detections:
    #                 event = {"timestamp": timestamp, "detections": detections}
    #                 self.memory_queue.append(event)
    #                 self.cloud.publish(event)          # <-- same event, straight to AWS

    #             #insert objects
    #             self.db.insert_multiple(self.memory_queue)
    #             self.memory_queue.clear()

    #             #update last timestamp
    #             self.last_logged_frame_timestamp = time.time()
                    

    #         with self.lock:
    #             #save the new frame and mark the new frame as complete
    #             self.inference_frame_ready = True
    #             self.inference_frame = self.inference_plotted

    def run(self):

        self.t_camera = threading.Thread(target = self.camera.task_camera, daemon = True)
        self.t_inference = threading.Thread(target = self.object_detect.task_inference, daemon = True)
        self.t_camera.start()
        self.t_inference.start()

        #Display message that the program was able to start tasks
        print("Program Starting. Press 'q' to quit.")

        #logic for FPS counter on screen
        #time since jan 1st, 1970. Actual value doesn't matter, what is needed is resolution
        last_frame_time_ns = time.time_ns()

        while not self.stop_event.is_set():

            if not HEADLESS:
                if self.inference_frame_ready.is_set():

                    #frame_time_calculation
                    new_frame_time_ns = time.time_ns()
                    frame_period_s = (new_frame_time_ns - last_frame_time_ns)/1e9
                    frame_freq = 1 / frame_period_s

                    last_frame_time_ns = new_frame_time_ns


                    #Call GUI display
                    self.gui.display_GUI(self.object_detect.getInferencePlot(), frame_freq)

            # Wait for 1 millisecond; if 'q' key is pressed, exit the loop
            if cv2.waitKey(1) & 0xFF == ord('q'):
                self.stopped = True

            time.sleep(0.001)

        # Flush any remaining items in the queue before shutting down
        # if self.memory_queue:
        #     self.db.insert_multiple(self.memory_queue)
        #     print("--> Flushed final remaining entries to TinyDB")

        # Clean up: End tasks, Release the camera hardware and destroy open windows
        #self.cloud.close()
        self.t_camera.join()
        self.t_inference.join()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    program = RasPiDeploy(src = 0)
    program.run()
    
