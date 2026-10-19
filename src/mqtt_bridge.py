import json
import sqlite3
import threading

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from iot_lab.sqlite_handler import SQLiteHandler
from iot_lab.timestamps import utc_timestamp

SENSOR_TYPE_DEFAULT = "esp32"   #used when a message doesn't name its own type
SENSOR_ID_DEFAULT = "E1"        #used when a message doesn't carry sensor_id


def message_to_reading(raw: bytes) -> dict | None:
    #Turn one MQTT message into a `readings` row, or None if it isn't usable.
    #The Pi stamps the time on receipt: networked sensors have no reliable clock
    #(docs/adr/0010-mqtt-sensor-ingest.md). sensor_id/sensor_type are pulled to
    #the envelope; whatever else the sensor sent becomes the measured `payload`.
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        print(f"[MQTT] dropped non-JSON message: {raw!r}")
        return None
    if not isinstance(data, dict):
        print(f"[MQTT] dropped non-object message: {data!r}")
        return None

    sensor_id = data.pop("sensor_id", SENSOR_ID_DEFAULT)
    sensor_type = data.pop("sensor_type", SENSOR_TYPE_DEFAULT)
    return {
        "ts": utc_timestamp(),
        "sensor_id": str(sensor_id),
        "sensor_type": str(sensor_type),
        "payload": data,
    }


class MQTTBridge:
    """
    Producer thread: subscribes to a local Mosquitto topic that networked
    sensors (e.g. an ESP32) publish to, and writes each message to the
    `readings` table (docs/adr/0010-mqtt-sensor-ingest.md).

    It is the MQTT-fed twin of DHTSensor: DHTSensor reads a sensor over GPIO,
    this reads sensors over MQTT, and both produce identical `readings` rows
    that the AWS forwarder uploads. Like DHTSensor it opens its own
    SQLiteHandler (WAL allows it alongside the event writer) and stops on
    stop_event.

    paho's network loop runs HERE via client.loop(): callbacks and the SQLite
    insert run on this one thread, so the sqlite3 connection stays
    single-threaded (loop_start would dispatch callbacks on paho's own thread).
    """

    def __init__(self, stop_event: threading.Event, db_path: str,
                 host = "localhost", port = 1883, topic = "sensors/+",
                 client_id = "mqtt-bridge", loop_timeout_s = 1.0, client = None):
        self.stop_event = stop_event
        self.db_path = db_path
        self.host = host
        self.port = port
        self.topic = topic
        #How long client.loop() blocks; also the max delay before we see stop_event
        self.loop_timeout_s = loop_timeout_s

        #Optional: set after every insert to wake the AWS forwarder
        self.inserted = None

        #Created here so a bad paho setup fails at startup; `client` lets tests
        #inject a fake. Connecting happens in the thread, not here.
        self.client = client if client is not None else mqtt.Client(
            CallbackAPIVersion.VERSION2, client_id=client_id
        )

    def task_bridge(self):
        #Own connection, opened in this thread (see SQLiteHandler docstring)
        try:
            db = SQLiteHandler(self.db_path)
        except Exception as e:
            print(f"[MQTT] could not open DB, bridge disabled: {e}")
            return

        def on_connect(client, userdata, flags, reason_code, properties=None, *args):
            failure = getattr(reason_code, "is_failure", (reason_code != 0))
            if failure:
                print(f"[MQTT] CONNECT failed: {reason_code}")
            else:
                client.subscribe(self.topic)
                print(f"[MQTT] connected, subscribed to '{self.topic}'")

        def on_message(client, userdata, msg):
            reading = message_to_reading(msg.payload)
            if reading is None:
                return
            try:
                db.insert_reading(reading)
            except sqlite3.Error as e:
                print(f"[MQTT] dropped reading {reading}: {e}")
            else:
                print(f"[MQTT] {msg.topic} -> readings: "
                      f"{reading['sensor_id']}/{reading['sensor_type']} {reading['payload']}")
                if self.inserted is not None:
                    self.inserted.set()

        self.client.on_connect = on_connect
        self.client.on_message = on_message

        try:
            self.client.connect(self.host, self.port)
        except Exception as e:
            print(f"[MQTT] could not reach broker {self.host}:{self.port}, bridge disabled: {e}")
            db.close()
            return

        try:
            #Run the network loop on THIS thread so callbacks share its DB
            #connection; wake to check stop_event every loop_timeout_s
            while not self.stop_event.is_set():
                self.client.loop(timeout=self.loop_timeout_s)
        finally:
            self.client.disconnect()
            db.close()
