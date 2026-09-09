# 1. Setup

```shell
python3 -m venv .venv
source .venv/bin/activate
pip install opencv-python ultralytics ncnn tinydb paho-mqtt python-dotenv
chmod +x start.sh
./start.sh
```
# 2. Run ncnn_export.py
```shell
python3 ncnn_export.py
```

# 3. Run camera.py
```shell
python3 camera.py
```

# 4. Do all the DynamoDB/IoT Core setup
sorry gang, you're on your own
