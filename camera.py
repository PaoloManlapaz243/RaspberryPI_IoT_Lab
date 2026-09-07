import time
import threading
import cv2
from ultralytics import YOLO

class RasPiDeploy:
    def __init__(self, src=0, model_dir = "./yolo11n_ncnn_model", height = 640, width = 480, conf_thresh = 0.3):

        # Load the exported NCNN model directory
        self.model = YOLO(model_dir, task = 'detect')

        # Initialize the Logitech USB camera (0 corresponds to /dev/video0)
        # Change the index to 1 or 2 if video0 doesn't display your webcam
        self.cap = cv2.VideoCapture(src)

        # Check if the webcam opened successfully
        if not self.cap.isOpened():
            print("Error: Could not open USB camera")
            exit()

        # Optional: Set preferred frame width and height
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, height)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, width)

        #Global data to pass
        self.conf_thresh = conf_thresh

        # Thread lock and stop
        self.lock = threading.Lock()
        self.stopped = False

        # Thread management state logic
        # For AI Inference Task
        # bool camera_frame_ready
        # Frame camera_frame
        # For GUI Display Task
        # bool AI_frame_ready
        # Frame AI_frame

        self.camera_frame_ready = False
        self.camera_frame = None
        self.inference_frame_ready = False
        self.inference_frame = None


    def task_camera(self):
        while not self.stopped:

            #attempt to get camera output
            #attempting this outside of lock to avoid using CPU time
            ret, frame = self.cap.read()

            #enter lock to modify state variables using with keyword
            with self.lock:
                # if successfully got a frame from the USB camera
                if ret:
                    self.camera_frame_ready = True
                    self.camera_frame = frame
                else:
                    self.camera_frame_ready = False
                    self.camera_frame = None

            # small delay to avoid CPU thrasing
            if not ret:
                time.sleep(0.001)


    def task_inference(self):
        while not self.stopped:
            #obtain the mutex to check the primitive variables
            with self.lock:
                #read in the new camera frame, set a local flag to use outside lock
                self.perform_Inference = self.camera_frame_ready
                self.frame_to_inference = self.camera_frame

                #mark the current frame as processed
                self.camera_frame_ready = False
                self.camera_frame = None

            # sleep to avoid CPU thrasing
            if not self.perform_Inference:
                time.sleep(0.001)
                continue

            self.inference_results = self.model(self.frame_to_inference, imgsz = 320, conf = self.conf_thresh, verbose = False)
            self.inference_plotted = self.inference_results[0].plot()

            with self.lock:
                #save the new frame and mark the new frame as complete
                self.inference_frame_ready = True
                self.inference_frame = self.inference_plotted

    def run(self):

        self.t_camera = threading.Thread(target = self.task_camera, daemon = True)
        self.t_inference = threading.Thread(target = self.task_inference, daemon = True)
        self.t_camera.start()
        self.t_inference.start()

        #Display message that the program was able to start tasks
        print("Program Starting. Press 'q' to quit.")

        #logic for FPS counter on screen
        self.last_frame = time.time_ns(); #time since jan 1st, 1970. Actual value doesn't matter, what is needed is resolution


        while not self.stopped:
            with self.lock:
                #read in the new inference frame
                self.perform_draw_frame = self.inference_frame_ready
                self.draw_frame = self.inference_frame

                #mark the frame as processed
                self.inference_frame_ready = False
                self.inference_frame = None

            if not self.perform_draw_frame:
                time.sleep(0.001)
                continue
            
            # If a new frame is successfully obtained, get the time for the new frame
            self.new_frame = time.time_ns();
            self.s_since_last_frame = (self.new_frame - self.last_frame) / 1e9; 
            self.fps = round(1/self.s_since_last_frame,2); #T=1/f

            #update last frame time
            self.last_frame = self.new_frame;

            #write to frame
            font = cv2.FONT_HERSHEY_SIMPLEX
            cv2.putText(self.draw_frame,"fps: " + str(self.fps),(500,20), font, 1,(255,255,255),2,cv2.LINE_AA)

            # Display the live stream in a window
            cv2.imshow('Baddie Alert', self.draw_frame)

            # Wait for 1 millisecond; if 'q' key is pressed, exit the loop
            if cv2.waitKey(1) & 0xFF == ord('q'):
                self.stopped = True

        # Clean up: End tasks, Release the camera hardware and destroy open windows
        self.t_camera.join()
        self.t_inference.join()
        self.cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    program = RasPiDeploy(src = 0)
    program.run()
    
