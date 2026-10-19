import os
import time
import threading
import cv2
from ultralytics import YOLO
from iot_lab.aws_publisher import AWSPublisher

import queue
from pathlib import Path
from dotenv import load_dotenv

from iot_lab.threads.camera import CameraHandler
from iot_lab.threads.object_detection import InferenceHandler
from iot_lab.threads.event_writer import EventWriter
from iot_lab.threads.aws_forwarder import AWSForwarder
from iot_lab.threads.dht_sensor import DHTSensor
from iot_lab.threads.mqtt_bridge import MQTTBridge
from iot_lab.timestamps import utc_timestamp
from iot_lab.gui_handler import GUIHandler



#Paths are anchored to this file, not the current directory, so the program
#works no matter where it is launched from
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = str(PROJECT_ROOT / "models" / "yolo11n_ncnn_model")

#SQLite event log
DB_PATH = str(PROJECT_ROOT / "logs" / "events.db")

#Identifies this device; also the DynamoDB partition key
SENSOR_ID = "S1"

#AWS IoT Core upload (store-and-forward, docs/adr/0002-store-and-forward.md).
#If enabled, a missing endpoint or cert is a config error and the app refuses
#to start; a network outage is normal and the app runs anyway.
ENABLE_AWS = True
AWS_TOPIC = "detections/events"
AWS_CLIENT_ID = "detector-01"
CERTS_DIR = PROJECT_ROOT / "certs"

#DHT11 temperature/humidity sensor (docs/adr/0009-sensor-readings.md). Pi
#only: enabling it without the hardware libraries is a config error.
ENABLE_DHT = False
DHT_SENSOR_ID = "T1"    #its own DynamoDB partition, separate from detections
DHT_PIN = "D4"          #Blinka pin name: GPIO4 (physical pin 7)

#Networked sensors over MQTT (docs/adr/0010-mqtt-sensor-ingest.md). A local
#Mosquitto broker on the Pi receives sensor messages (e.g. from an ESP32) and
#this bridge writes them to the readings table. Pi-side only; needs the broker.
ENABLE_MQTT = False
MQTT_HOST = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC = "sensors/+"   #one level under sensors/: sensors/esp32, sensors/...

#AWS_ENDPOINT lives in .env (kept out of git)
load_dotenv(PROJECT_ROOT / ".env")

#Set True for headless setup for greater performance
HEADLESS = False

class RasPiDeploy:
    def __init__(self, src=0, model_dir = MODEL_DIR, height = 640, width = 480, conf_thresh = 0.3):

        #db logs
        #self.db = TinyDB("logs/camera_logs.json")
        #self.memory_queue = []
        #self.last_logged_frame_timestamp = time.time()

        #AWS upload (None when disabled). Built first, so a config error fails
        #before the camera and model are loaded.
        self.publisher = self._make_publisher()
        self.forwarder = AWSForwarder(DB_PATH, self.publisher) if self.publisher else None

        #Global data to pass
        self.conf_thresh = conf_thresh

        # Thread lock and stop
        self.lock = threading.Lock()

        # New Event-based locking
        self.stop_event = threading.Event()
        self.stop_event.clear()


        #Thread-safe datastructure: detection events, inference -> event writer
        self.queue = queue.Queue()

        #Tracker IDs restart at 1 every run; (run_id, track_id) identifies a track
        self.run_id = utc_timestamp()
        
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
            conf_thresh,
            sensor_id = SENSOR_ID,
            run_id = self.run_id
        )

        #Initialize Event Writer (opens the DB inside its own thread)
        #inserted: wakes the forwarder after each insert so uploads are immediate
        self.event_writer = EventWriter(
            self.queue,
            DB_PATH,
            inserted = self.forwarder.wakeup if self.forwarder else None
        )

        #Initialize DHT sensor (None when disabled). It writes readings to SQLite
        #itself and wakes the forwarder, like the event writer does
        self.dht = DHTSensor(
            self.stop_event,
            DB_PATH,
            sensor_id = DHT_SENSOR_ID,
            pin = DHT_PIN,
            inserted = self.forwarder.wakeup if self.forwarder else None
        ) if ENABLE_DHT else None

        #Initialize MQTT bridge (None when disabled). Mirrors the DHT sensor:
        #its own thread + SQLiteHandler, writes readings, wakes the forwarder
        self.mqtt = MQTTBridge(
            self.stop_event,
            DB_PATH,
            host = MQTT_HOST,
            port = MQTT_PORT,
            topic = MQTT_TOPIC,
        ) if ENABLE_MQTT else None
        if self.mqtt:
            self.mqtt.inserted = self.forwarder.wakeup if self.forwarder else None

        #Initialize GUI Handler
        self.gui = GUIHandler(
            self.inference_frame_ready,
            self.stop_event
        )


    def _make_publisher(self):
        if not ENABLE_AWS:
            print("[AWS] disabled (ENABLE_AWS = False); logging to SQLite only")
            return None

        endpoint = os.getenv("AWS_ENDPOINT")
        if not endpoint:
            raise RuntimeError("ENABLE_AWS is True but AWS_ENDPOINT is not set in .env")

        #Missing cert files raise FileNotFoundError here (fail fast on config)
        return AWSPublisher(
            endpoint = endpoint,
            ca_path = str(CERTS_DIR / "AmazonRootCA1.pem"),
            cert_path = str(CERTS_DIR / f"{AWS_CLIENT_ID}.cert.pem"),
            key_path = str(CERTS_DIR / f"{AWS_CLIENT_ID}.private.key"),
            topic = AWS_TOPIC,
            client_id = AWS_CLIENT_ID
        )

    def run(self):

        self.t_camera = threading.Thread(target = self.camera.task_camera, daemon = True)
        self.t_inference = threading.Thread(target = self.object_detect.task_inference, daemon = True)
        self.t_writer = threading.Thread(target = self.event_writer.task_writer, daemon = True)
        self.t_writer.start()

        #Fail fast: if the DB can't be opened, don't run detection whose events
        #would pile up in the queue and be lost
        if not self.event_writer.wait_until_ready():
            raise RuntimeError(f"Event writer failed to start: {self.event_writer.error}")

        if self.forwarder:
            self.t_forwarder = threading.Thread(target = self.forwarder.task_forwarder, daemon = True)
            self.t_forwarder.start()

        self.t_camera.start()
        self.t_inference.start()

        if self.dht:
            self.t_dht = threading.Thread(target = self.dht.task_sensor, daemon = True)
            self.t_dht.start()

        if self.mqtt:
            self.t_mqtt = threading.Thread(target = self.mqtt.task_bridge, daemon = True)
            self.t_mqtt.start()

        #Display message that the program was able to start tasks
        print(f"Program Starting (run_id {self.run_id}). Press 'q' (or Ctrl+C) to quit.")

        try:
            self._main_loop()
        except KeyboardInterrupt:
            #Ctrl+C lands on the main thread; fall through to the clean shutdown
            pass
        finally:
            self.shutdown()

    def _main_loop(self):

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
                    self.inference_frame_ready.clear()

            # Wait for 1 millisecond; if 'q' key is pressed, exit the loop
            if cv2.waitKey(1) & 0xFF == ord('q'):
                self.stop_event.set()

            time.sleep(0.001)

    def shutdown(self):
        #Order matters (docs/adr/0001-event-pipeline.md):
        #1. stop producers; inference emits exit events for remaining tracks
        self.stop_event.set()
        self.t_camera.join()
        self.t_inference.join()
        if self.dht:
            self.t_dht.join()
        if self.mqtt:
            self.t_mqtt.join()

        #2. only now queue the sentinel, so those final exits are written first
        self.event_writer.stop()
        self.t_writer.join()

        #3. every event is now in SQLite; let the forwarder make a final bounded
        #   upload attempt (anything left is sent next run), then close MQTT
        if self.forwarder:
            self.forwarder.stop()
            self.t_forwarder.join()
            self.publisher.close()

        cv2.destroyAllWindows()
        print("Shutdown complete.")


if __name__ == "__main__":
    program = RasPiDeploy(src = 0)
    program.run()
    
