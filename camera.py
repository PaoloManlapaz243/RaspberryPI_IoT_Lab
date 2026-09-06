import cv2

# Initialize the Logitech USB camera (0 corresponds to /dev/video0)
# Change the index to 1 or 2 if video0 doesn't display your webcam
cap = cv2.VideoCapture(0)

# Optional: Set preferred frame width and height
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

# Check if the webcam opened successfully
if not cap.isOpened():
    print("Error: Could not open the USB camera.")
    exit()

print("Streaming started. Press 'q' to quit.")

while True:
    # Capture frame-by-frame
    ret, frame = cap.read()
    
    # If the frame was not grabbed correctly, break the loop
    if not ret:
        print("Error: Failed to grab frame.")
        break

    # Display the live stream in a window
    cv2.imshow('Baddie Alert', frame)

    # Wait for 1 millisecond; if 'q' key is pressed, exit the loop
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

# Clean up: Release the camera hardware and destroy open windows
cap.release()
cv2.destroyAllWindows()
