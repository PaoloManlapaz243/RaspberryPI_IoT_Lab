import time
import cv2
from ultralytics import YOLO

# 1. Load the exported NCNN model directory
model = YOLO("./yolo11n_ncnn_model")

# Initialize the Logitech USB camera (0 corresponds to /dev/video0)
# Change the index to 1 or 2 if video0 doesn't display your webcam
#cap = cv2.VideoCapture(0)
cap = cv2.VideoCapture("Golden.webm")

# Optional: Set preferred frame width and height
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

# Check if the webcam opened successfully
if not cap.isOpened():
    print("Error: Could not open the USB camera.")
    exit()

print("Streaming started. Press 'q' to quit.")

#logic for FPS counter on screen
last_frame = time.time_ns(); #time since jan 1st, 1970. Actual value doesn't matter, what is needed is resolution


while True:
    # Capture frame-by-frame
    ret, frame = cap.read()
    
    # If the frame was not grabbed correctly, break the loop
    if not ret:
        print("Error: Failed to grab frame.")
        break

    # 3. Run NCNN inference (imgsz=320 matches our export size)
    results = model(frame, imgsz=320, conf=0.3, verbose=False)

    # 4. Render bounding boxes onto the frame
    frame = results[0].plot()

    # If a new frame is successfully obtained, get the time for the new frame
    new_frame = time.time_ns();
    ns_since_last_frame = new_frame - last_frame;
    s_since_last_frame = ns_since_last_frame / 1e9; 
    fps = round(1/s_since_last_frame,2); #T=1/f

    #update last frame time
    last_frame = new_frame;

    #write to frame
    font = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(frame,"fps: " + str(fps),(500,20), font, 1,(255,255,255),2,cv2.LINE_AA)

    # Display the live stream in a window
    cv2.imshow('Baddie Alert', frame)

    # Wait for 1 millisecond; if 'q' key is pressed, exit the loop
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

# Clean up: Release the camera hardware and destroy open windows
cap.release()
cv2.destroyAllWindows()
