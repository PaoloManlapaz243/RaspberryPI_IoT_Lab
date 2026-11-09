"""commands.py (ADR 0012): the guard, issue()'s log + publish order, and the
publisher's ack matching. A fake paho client stands in for Mosquitto and the
ESP32, so no broker or network is needed."""
import json
import threading
from types import SimpleNamespace

import paho.mqtt.client as mqtt
import pytest

from iot_lab import commands


# ---------------- the guard ----------------

@pytest.mark.parametrize("question, state", [
    ("turn the cooling on", "on"),
    ("turn on the fan", "on"),
    ("please switch the AC on", "on"),
    ("can you start the fan?", "on"),
    ("cool the room down with the fan", "on"),
    ("turn off the cooling", "off"),
    ("switch the fan off please", "off"),
    ("stop the AC", "off"),
    ("shut the fan off", "off"),
])
def test_explicit_requests_are_allowed(question, state):
    commands.check(question, "cooling", state)   #does not raise

@pytest.mark.parametrize("question, state, why", [
    #The model decided on its own: the user never asked for a command
    ("it's really hot in here", "on", "didn't ask for the cooling"),
    ("what's the temperature?", "on", "didn't ask for the cooling"),
    ("is the fan running?", "on", "didn't ask to turn"),
    #Advice, how-to, negation and conditions are not requests
    ("should I turn on the fan?", "on", "not a request"),
    ("how do I turn the fan on?", "on", "not a request"),
    ("don't turn on the fan", "on", "not a request"),
    ("turn on the fan if it gets hot", "on", "not a request"),
    #The model flipped the state the user asked for
    ("turn the fan off", "on", "not just on"),
    #Ambiguous: both states
    ("turn the fan on then off", "on", "not just on"),
])
def test_anything_else_is_blocked(question, state, why):
    with pytest.raises(commands.CommandBlocked, match=why):
        commands.check(question, "cooling", state)

def test_only_allow_listed_actuators_and_states():
    with pytest.raises(commands.CommandBlocked, match="unknown actuator"):
        commands.check("turn the heater on", "heater", "on")
    with pytest.raises(commands.CommandBlocked, match="can't be set"):
        commands.check("set the fan to max", "cooling", "max")


# ---------------- issue(): guard -> log -> publish ----------------

def _log(path):
    return [json.loads(line) for line in path.read_text().splitlines()]

def test_allowed_command_is_published_and_logged(tmp_path):
    pub = commands.DryRunPublisher()
    result = commands.issue("turn the fan on", "cooling", "on", pub, tmp_path / "c.jsonl")
    assert result["status"] == "dry_run"
    assert pub.sent[0]["actuator"] == "cooling" and pub.sent[0]["state"] == "on"
    entry = _log(tmp_path / "c.jsonl")[0]
    assert entry["status"] == "dry_run" and entry["command_id"] == pub.sent[0]["command_id"]

def test_blocked_command_never_reaches_the_publisher_but_is_logged(tmp_path):
    class Exploding:
        def send(self, command):
            raise AssertionError("published a blocked command")
    result = commands.issue("it's hot", "cooling", "on", Exploding(), tmp_path / "c.jsonl")
    assert "declined" in result
    assert _log(tmp_path / "c.jsonl")[0]["status"] == "blocked"

def test_broker_failure_is_reported_not_raised(tmp_path):
    class Down:
        def send(self, command):
            raise ConnectionRefusedError("no broker")
    result = commands.issue("turn the fan on", "cooling", "on", Down(), tmp_path / "c.jsonl")
    assert result["status"] == "failed" and "no broker" in result["error"]
    assert "couldn't send" in commands.describe(result)

@pytest.mark.parametrize("status, words", [
    ("confirmed", "confirmed by the ESP32"), ("sent", "didn't confirm"),
    ("rejected", "refused"), ("dry_run", "Dry run"),
])
def test_describe_is_exact_per_outcome(status, words):
    assert words in commands.describe({"actuator": "cooling", "state": "on", "status": status})


# ---------------- CommandPublisher, with a fake broker + ESP32 ----------------

class FakeClient:
    """Acts like paho + Mosquitto + the ESP32: acks each command on the ack
    topic from another thread, as paho's network thread would."""
    def __init__(self, ack=None):
        self.ack = ack            #None = the ESP32 never answers
        self.published = []

    def connect(self, host, port, keepalive=60): pass
    def loop_start(self): pass
    def loop_stop(self): pass
    def disconnect(self): pass

    def subscribe(self, topic, qos=0):
        self.on_subscribe(self, None, 1)

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, json.loads(payload)))
        if self.ack is not None:
            reply = {"command_id": json.loads(payload)["command_id"], **self.ack}
            msg = SimpleNamespace(topic=commands.ACK_TOPIC, payload=json.dumps(reply).encode())
            threading.Timer(0.01, self.on_message, (self, None, msg)).start()
        return SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS, mid=1)

def test_ack_confirms_the_command():
    client = FakeClient(ack={"ok": True})
    pub = commands.CommandPublisher(client=client, ack_timeout_s=1.0)
    assert pub.send(commands.make_command("cooling", "on")) == {"status": "confirmed"}
    assert client.published[0][0] == commands.COMMAND_TOPIC

def test_device_rejection_is_reported():
    pub = commands.CommandPublisher(client=FakeClient(ack={"ok": False, "error": "bad state"}),
                                    ack_timeout_s=1.0)
    assert pub.send(commands.make_command("cooling", "on")) == {"status": "rejected",
                                                                "error": "bad state"}

def test_no_ack_means_sent_not_confirmed():
    pub = commands.CommandPublisher(client=FakeClient(ack=None), ack_timeout_s=0.05)
    assert pub.send(commands.make_command("cooling", "on")) == {"status": "sent"}
