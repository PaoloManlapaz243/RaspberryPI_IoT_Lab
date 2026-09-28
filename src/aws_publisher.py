# import json
# import ssl
# import paho.mqtt.client as mqtt


# class AWSPublisher:
#     """
#     Thin MQTT publisher to AWS IoT Core. Does ONE job: take an event dict and
#     publish it as JSON to a topic. All detection extraction, throttling, and
#     TinyDB writing stays in camera.py -- this only adds the cloud hop.
#     An IoT Rule on the AWS side routes each published message into DynamoDB.
#     """

#     def __init__(self, endpoint, ca_path, cert_path, key_path,
#                  topic="detections/events", sensor_id="S1",
#                  client_id="laptop-dev"):
#         self.topic = topic
#         self.sensor_id = sensor_id

#         # paho-mqtt 2.x: callback API version is the first arg.
#         # If you're on paho 1.x, remove that first argument.
#         self.client = mqtt.Client(
#             mqtt.CallbackAPIVersion.VERSION2,
#             client_id=client_id,
#         )
#         # Mutual TLS to IoT Core: root CA + device cert + private key.
#         self.client.tls_set(
#             ca_certs=ca_path,
#             certfile=cert_path,
#             keyfile=key_path,
#             tls_version=ssl.PROTOCOL_TLSv1_2,
#         )
#         self.client.connect(endpoint, 8883)   # 8883 = MQTT over TLS
#         # Background network thread -> publish() is non-blocking, so it won't
#         # stall your inference thread.
#         self.client.loop_start()

#     def publish(self, event):
#         """
#         event = the dict you already build:
#             {"timestamp": "...", "detections": [...]}
#         We add sensor_id because DynamoDB needs the table's partition key
#         present as a top-level field.
#         """
#         payload = {"sensor_id": self.sensor_id, **event}
#         try:
#             self.client.publish(self.topic, json.dumps(payload), qos=1)
#         except Exception as e:
#             print("MQTT publish failed, kept local copy:", e)

#     def close(self):
#         self.client.loop_stop()
#         self.client.disconnect()

import json
import ssl
import threading
import time
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion


class AWSPublisher:
    """
    Thin MQTT publisher to AWS IoT Core. publish() never blocks: paho queues the
    message and its own network thread (loop_start) sends it, reconnecting and
    retrying in the background if the network drops.

    Durability is NOT this class's job: the AWSForwarder only publishes while
    connected, and re-sends from SQLite anything that wasn't acknowledged.
    """

    def __init__(self, endpoint, ca_path, cert_path, key_path,
                 topic="detections/events", sensor_id="S1",
                 client_id="laptop-dev"):
        self.topic = topic
        self.sensor_id = sensor_id
        self.connected = False

        #Message IDs published but not yet acknowledged by the broker.
        #Added from the caller's thread, removed from paho's network thread,
        #so guard with a lock.
        self._pending = set()
        #Acks that arrived before publish() recorded their mid (see publish)
        self._acked_early = set()
        self._pending_lock = threading.Lock()

        self.client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_publish = self._on_publish

        #Bad cert paths raise here, immediately: that's a config error, not a
        #network problem, so failing fast is the right call
        self.client.tls_set(
            ca_certs=ca_path,
            certfile=cert_path,
            keyfile=key_path,
            tls_version=ssl.PROTOCOL_TLSv1_2,
        )

        #Retry with backoff (1s, 2s, 4s ... capped at 60s) while unreachable
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)

        #connect_async + loop_start: the network thread connects in the
        #background and keeps retrying, even if the FIRST attempt fails. A Pi
        #that boots without network still starts and logs locally.
        print(f"[AWS] connecting to {endpoint}:8883 as client_id='{client_id}' (in background)")
        self.client.connect_async(endpoint, 8883)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None, *args):
        # reason_code is a ReasonCode in paho 2.x; fall back to int compare otherwise.
        failure = getattr(reason_code, "is_failure", (reason_code != 0))
        if failure:
            print(f"[AWS] CONNECT REFUSED: {reason_code}. Usually the cert isn't "
                  "ACTIVE, the policy isn't attached, or the policy doesn't allow "
                  "this client_id.")
        else:
            self.connected = True
            print(f"[AWS] connected OK ({reason_code})")

    def _on_disconnect(self, client, userdata, *args):
        self.connected = False
        print(f"[AWS] DISCONNECTED {args}. If this fires right after a publish, "
              "your policy almost certainly doesn't allow publishing to "
              f"'{self.topic}'.")

    def _on_publish(self, client, userdata, mid, *args):
        #QoS 1: fires when the broker acknowledges (PUBACK) the message
        with self._pending_lock:
            if mid in self._pending:
                self._pending.remove(mid)
            else:
                self._acked_early.add(mid)

    def publish(self, event) -> bool:
        #Returns True if paho accepted the message (it will be delivered, even
        #across a reconnect); False if it was rejected outright
        payload = {"sensor_id": self.sensor_id, **event}

        #Never hold _pending_lock while calling into paho: paho calls
        #on_publish while holding its own internal lock, so holding ours here
        #too would risk a lock-order deadlock between the two threads
        info = self.client.publish(self.topic, json.dumps(payload), qos=1)

        #NO_CONN is not a failure: paho keeps the message and sends it after
        #reconnecting. Anything else means it was never queued.
        if info.rc not in (mqtt.MQTT_ERR_SUCCESS, mqtt.MQTT_ERR_NO_CONN):
            print(f"[AWS] publish FAILED (rc={info.rc}): {mqtt.error_string(info.rc)}")
            return False

        with self._pending_lock:
            #The ack can race ahead of this line on a fast connection
            if info.mid in self._acked_early:
                self._acked_early.remove(info.mid)
            else:
                self._pending.add(info.mid)
        return True

    def wait_for_acks(self, timeout: float) -> bool:
        """
        Block until the broker has acknowledged everything published so far.
        Returns False on timeout or if the connection drops, so the caller can
        retry later. (paho's own wait_for_publish()/is_published() raise for
        messages queued while offline, even after they're delivered, so we
        track acks ourselves in _on_publish.)
        """
        deadline = time.monotonic() + timeout
        while self._pending_count():
            if not self.connected or time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        return True

    def close(self, timeout=3.0):
        #Order matters: loop_stop() kills the network thread and does NOT
        #guarantee queued messages were sent. Wait for acks first, then
        #disconnect, then stop the thread.
        if not self.wait_for_acks(timeout):
            print(f"[AWS] WARNING: closing with {self._pending_count()} unacknowledged "
                  "event(s); they are still in SQLite and will be re-sent next run")

        self.client.disconnect()
        self.client.loop_stop()

    def _pending_count(self):
        with self._pending_lock:
            return len(self._pending)
