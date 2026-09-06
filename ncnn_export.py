from ultralytics import YOLO

# Load the nano PyTorch model
model = YOLO("yolo11n.pt")

# Export to NCNN format with an optimized input resolution (320x320 for speed)
# This creates a folder named 'yolo11n_ncnn_model'
model.export(format="ncnn", imgsz=320)
print("Export complete! 'yolo11n_ncnn_model' is ready for Pi inference.")