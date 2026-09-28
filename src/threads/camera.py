import cv2
import threading
import time

class CameraHandler:
    def __init__(self, frame_event: threading.Event, stop_event: threading.Event, src = 0, width = 480, height = 640):

        #Initialize logitech USB Camera (0 is /dev/video0)
        #If 0 doesnt work, try setting to 1 or 2
        self.cap = cv2.VideoCapture(src)

        if not self.cap.isOpened():
            print("Could not open USB Camera")
            exit()

        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

        #Thread-safe State Variables
        self.camera_frame_event = frame_event
        self.stop_event = stop_event

        #Variable for camera frame
        self.camera_frame = None

    def task_camera(self):
        while not self.stop_event.is_set():
            ret, frame = self.cap.read()

            if ret:
                self.camera_frame = frame
                self.camera_frame_event.set()
            else:
                self.camera_frame = None
                self.camera_frame_event.clear()
                time.sleep(0.001)

    def get_frame(self):
        return self.camera_frame

    def __del__(self):
        self.cap.release()
            