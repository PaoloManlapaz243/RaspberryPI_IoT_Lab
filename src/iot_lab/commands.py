"""
Commands DOWN to the ESP32 (docs/adr/0012-command-and-control.md).

    agent --set_cooling--> issue() --guard, log--> MQTT commands/esp32 --> ESP32 relay
                                                   MQTT sensors/esp32/ack <-- ESP32

The safety boundary is issue(): it is the ONLY function that publishes a
command, and it always runs the guard and writes the log first, whoever calls
it. The guard allows a command only if:
  1. the actuator and state are on the allow-list (ACTUATORS), and
  2. the USER'S OWN WORDS explicitly ask for it ("turn the cooling on").
The model can propose a command; it can never authorize one. Neither can text
the model read (a doc passage, a sensor payload): only the question is checked.

Every attempt, blocked or sent, is appended to logs/commands.jsonl.
"""

import json
import re
import threading
import uuid
from pathlib import Path

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from iot_lab.timestamps import utc_timestamp

# ---------------- config ----------------
PROJECT_ROOT  = Path(__file__).resolve().parents[2]
COMMAND_LOG   = PROJECT_ROOT / "logs" / "commands.jsonl"   # gitignored

BROKER_HOST   = "localhost"          # Mosquitto on the Pi (the hotspot broker)
BROKER_PORT   = 1883
COMMAND_TOPIC = "commands/esp32"
ACK_TOPIC     = "sensors/esp32/ack"  # not matched by the bridge's sensors/+ (one level)
CLIENT_ID     = "agent-commands"     # unique on the broker
ACK_TIMEOUT_S = 3.0

#The allow-list: actuator -> states it accepts. The ESP32 sketch checks the
#same list again (defense in depth).
ACTUATORS = {"cooling": {"on", "off"}}

#Words that name each actuator in a request
ACTUATOR_WORDS = {
    "cooling": re.compile(r"\b(cooling|cooler|fan|a/?c|air[- ]?con(ditioning|ditioner)?)\b"),
}

#Words that ask for each state. "on" also accepts "cool it/the room down".
STATE_WORDS = {
    "on":  re.compile(r"\b(turn|switch|put|flip|power)\s+(\w+\s+){0,2}on\b"
                      r"|\b(turn|switch|power)\s+on\b|\b(start|enable|activate)\b"
                      r"|\bcool\s+(it|the\s+room|things|us)\s+down\b"),
    "off": re.compile(r"\b(turn|switch|put|flip|power|shut)\s+(\w+\s+){0,2}off\b"
                      r"|\b(turn|switch|power|shut)\s+off\b|\b(stop|disable|deactivate)\b"),
}

#Phrasings that are about a command, not a request for one: advice, how-to,
#negation. "should I turn on the fan?" gets a recommendation, not a relay click.
NOT_A_REQUEST = re.compile(
    r"\b(should|shall|would|must)\s+(i|we|it)\b|\bdo\s+(you|i|we)\s+(think|need)\b"
    r"|\brecommend|\bworth\b|\bwhat\s+if\b|\bwhy\b|\bhow\s+(do|does|can|would|should)\b"
    r"|\bdon'?t\b|\bdo\s+not\b|\bnever\b|\bno\s+need\b|\bwhen\b|\bif\b")


class CommandBlocked(Exception):
    pass


def check(question, actuator, state):
    """Raise CommandBlocked unless this exact command is allowed AND explicitly
    requested in the user's question. Pure: no I/O."""
    if actuator not in ACTUATORS:
        raise CommandBlocked(f"unknown actuator {actuator!r}")
    if state not in ACTUATORS[actuator]:
        raise CommandBlocked(f"{actuator} can't be set to {state!r}")

    q = question.lower()
    if not ACTUATOR_WORDS[actuator].search(q):
        raise CommandBlocked(f"you didn't ask for the {actuator}")
    if NOT_A_REQUEST.search(q):
        raise CommandBlocked("that reads as a question or a condition, not a request")
    asked = {s for s, pattern in STATE_WORDS.items() if pattern.search(q)}
    #Switching ON is the riskier direction: any bare "off" makes it ambiguous
    #("turn the fan on then off"). Not mirrored for OFF: "on" is too common
    #("the fan on the desk"), and an unwanted off is the safe failure.
    if state == "on" and re.search(r"\boff\b", q):
        asked.add("off")
    if asked != {state}:
        if not asked:
            raise CommandBlocked(f"you didn't ask to turn the {actuator} {state}")
        raise CommandBlocked(f"you asked for {' and '.join(sorted(asked))}, not just {state}")


def make_command(actuator, state):
    return {"command_id": uuid.uuid4().hex[:12], "actuator": actuator, "state": state,
            "ts": utc_timestamp(), "source": "agent"}


def _log(log_path, entry):
    if log_path is None:
        return
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def issue(question, actuator, state, publisher, log_path=COMMAND_LOG):
    """The one way to send a command: guard -> log -> publish -> log the outcome.
    Returns a result dict for the agent: {"status": ...} or {"declined": ...}.
    `publisher` is only touched once the guard passes (it may connect lazily)."""
    entry = {"ts": utc_timestamp(), "question": question, "actuator": actuator, "state": state}
    try:
        check(question, actuator, state)
    except CommandBlocked as e:
        _log(log_path, {**entry, "status": "blocked", "reason": str(e)})
        return {"actuator": actuator, "state": state,
                "declined": f"no command sent: {e}. Ask explicitly, e.g. "
                            f"'turn the {actuator} {state}'"}

    command = make_command(actuator, state)
    try:
        outcome = publisher.send(command)
    except Exception as e:   #broker down, etc.: report it, never crash the agent
        outcome = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
    _log(log_path, {**entry, "command_id": command["command_id"], **outcome})
    return {"actuator": actuator, "state": state, "command_id": command["command_id"], **outcome}


def describe(result):
    """A command result -> one exact sentence, written by code (the model never
    phrases what a machine did)."""
    a, s = result.get("actuator", "?"), result.get("state", "?")
    if "declined" in result:
        return f"I didn't send a command ({result['declined']})."
    status = result.get("status")
    if status == "confirmed":
        return f"The {a} is now {s} (confirmed by the ESP32)."
    if status == "sent":
        return (f"I sent '{a} {s}' to the ESP32, but it didn't confirm within "
                f"{ACK_TIMEOUT_S:g} s, so it may not have switched.")
    if status == "rejected":
        return f"The ESP32 refused '{a} {s}': {result.get('error', 'no reason given')}."
    if status == "dry_run":
        return f"Dry run: would have sent '{a} {s}' to the ESP32."
    return f"I couldn't send '{a} {s}': {result.get('error', 'unknown error')}."


# ============ transport ============

class CommandPublisher:
    """Publishes commands to the local Mosquitto broker and waits for the
    ESP32's ack. Same paho patterns as AWSPublisher (VERSION2 callbacks,
    loop_start, QoS 1), but plain TCP on the closed hotspot instead of TLS to
    IoT Core. Connects on the first send, so the agent starts without a broker."""

    def __init__(self, host=BROKER_HOST, port=BROKER_PORT, topic=COMMAND_TOPIC,
                 ack_topic=ACK_TOPIC, client_id=CLIENT_ID, ack_timeout_s=ACK_TIMEOUT_S,
                 client=None):
        self.host, self.port = host, port
        self.topic, self.ack_topic = topic, ack_topic
        self.ack_timeout_s = ack_timeout_s
        self.client = client if client is not None else mqtt.Client(
            CallbackAPIVersion.VERSION2, client_id=client_id)
        self.client.on_message = self._on_message
        self.client.on_subscribe = self._on_subscribe
        self._subscribed = threading.Event()
        self._started = False
        #command_id -> [Event, ack dict]; written from paho's thread
        self._waiting = {}
        self._lock = threading.Lock()

    def _start(self):
        if self._started:
            return
        #Synchronous connect: a missing broker is an immediate, reportable error
        self.client.connect(self.host, self.port, keepalive=30)
        self.client.loop_start()
        try:
            self.client.subscribe(self.ack_topic, qos=1)
            #Subscribe before the first publish, or a fast ack could be missed
            if not self._subscribed.wait(self.ack_timeout_s):
                raise TimeoutError(f"no SUBACK for {self.ack_topic}")
        except Exception:
            #Undo, so the next send starts clean instead of on a half-open client
            self.client.disconnect()
            self.client.loop_stop()
            raise
        self._started = True

    def _on_subscribe(self, client, userdata, mid, *args):
        self._subscribed.set()

    def _on_message(self, client, userdata, msg):
        try:
            ack = json.loads(msg.payload)
            waiter = self._waiting.get(ack.get("command_id")) if isinstance(ack, dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if waiter is not None:
            with self._lock:
                waiter[1] = ack
            waiter[0].set()

    def send(self, command):
        self._start()
        waiter = [threading.Event(), None]
        self._waiting[command["command_id"]] = waiter
        try:
            info = self.client.publish(self.topic, json.dumps(command), qos=1)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                return {"status": "failed", "error": mqtt.error_string(info.rc)}
            if not waiter[0].wait(self.ack_timeout_s):
                return {"status": "sent"}
            with self._lock:
                ack = waiter[1]
            if ack.get("ok") is True:
                return {"status": "confirmed"}
            return {"status": "rejected", "error": str(ack.get("error", "not ok"))}
        finally:
            self._waiting.pop(command["command_id"], None)

    def close(self):
        if self._started:
            self.client.disconnect()
            self.client.loop_stop()


class DryRunPublisher:
    """Sends nothing; records what would have been sent (evals, demos without
    the ESP32)."""
    def __init__(self):
        self.sent = []

    def send(self, command):
        self.sent.append(command)
        return {"status": "dry_run"}
