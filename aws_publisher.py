import json
import ssl
import paho.mqtt.client as mqtt


class AWSPublisher:
    """
    Thin MQTT publisher to AWS IoT Core. Does ONE job: take an event dict and
    publish it as JSON to a topic. All detection extraction, throttling, and
    TinyDB writing stays in camera.py -- this only adds the cloud hop.
    An IoT Rule on the AWS side routes each published message into DynamoDB.
    """

    def __init__(self, endpoint, ca_path, cert_path, key_path,
                 topic="detections/events", sensor_id="S1",
                 client_id="laptop-dev"):
        self.topic = topic
        self.sensor_id = sensor_id

        # paho-mqtt 2.x: callback API version is the first arg.
        # If you're on paho 1.x, remove that first argument.
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
        # Mutual TLS to IoT Core: root CA + device cert + private key.
        self.client.tls_set(
            ca_certs=ca_path,
            certfile=cert_path,
            keyfile=key_path,
            tls_version=ssl.PROTOCOL_TLSv1_2,
        )
        self.client.connect(endpoint, 8883)   # 8883 = MQTT over TLS
        # Background network thread -> publish() is non-blocking, so it won't
        # stall your inference thread.
        self.client.loop_start()

    def publish(self, event):
        """
        event = the dict you already build:
            {"timestamp": "...", "detections": [...]}
        We add sensor_id because DynamoDB needs the table's partition key
        present as a top-level field.
        """
        payload = {"sensor_id": self.sensor_id, **event}
        try:
            self.client.publish(self.topic, json.dumps(payload), qos=1)
        except Exception as e:
            print("MQTT publish failed, kept local copy:", e)

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()