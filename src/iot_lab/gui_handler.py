import cv2
import threading

class GUIHandler:
    def __init__(self, inference_plot_ready: threading.Event, stopped: threading.Event):

        #State management variables
        self.inference_plot_ready = inference_plot_ready
        self.stopped = stopped


    def display_GUI(self, frame, fps):

        frame_to_draw = frame
        frames_per_second = fps

        font = cv2.FONT_HERSHEY_SIMPLEX
        cv2.putText(frame_to_draw, f'fps: {frames_per_second}', (500,20), font, 1,(255,255,255),2,cv2.LINE_AA)

        cv2.imshow('Baddie Alert', frame_to_draw)


    def __del__(self):
        cv2.destroyAllWindows()