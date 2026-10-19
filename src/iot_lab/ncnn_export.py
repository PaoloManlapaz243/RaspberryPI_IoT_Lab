from pathlib import Path
from ultralytics import YOLO

#Paths are anchored to this file, not the current directory (like main.py)
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Load the nano PyTorch model
model = YOLO(str(PROJECT_ROOT / "models" / "yolo11n.pt"))

# Export to NCNN format with an optimized input resolution (320x320 for speed)
# This creates models/yolo11n_ncnn_model/, next to the .pt file
model.export(format="ncnn", imgsz=320)
print("Export complete! 'yolo11n_ncnn_model' is ready for Pi inference.")
