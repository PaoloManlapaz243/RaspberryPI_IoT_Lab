"""
DHT11 temperature + humidity reader. SEPARATE PROCESS (like detection_assistant.py).

It writes sensor readings into the SAME logs/events.db that main.py runs. The
AWS forwarder in main.py drains the `readings` table and uploads to the cloud
(docs/adr/0008-sensor-readings.md) -- so this process only has to write locally;
it does no networking itself. Readings upload within the forwarder's poll
interval (~1 s) while main.py is running.

Wiring (3-pin module -> Pi):  VCC->3.3V(pin1)  DATA->GPIO4(pin7)  GND->GND(pin6)
Bare 4-pin sensor: add a 10k pull-up between DATA and VCC.

Run on the Pi (in the venv), alongside main.py:
    source .venv/bin/activate
    python src/dht_reader.py
"""

import time
from pathlib import Path
from datetime import datetime, timezone

from iot_lab.sqlite_handler import SQLiteHandler

# ---------------- config ----------------
#Same DB main.py uses; anchored to this file so it runs from any directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = str(PROJECT_ROOT / "logs" / "events.db")

SENSOR_ID   = "T1"       # DynamoDB partition key; separates this sensor's rows
SENSOR_TYPE = "dht11"    # discriminator stored with every reading
READ_EVERY  = 3.0        # seconds between readings (DHT11 wants >= ~2 s)
DHT_PIN     = "D4"       # Blinka board pin name -> GPIO4 (physical pin 7)


def utc_ms() -> str:
    #UTC ISO8601 with milliseconds, e.g. 2026-10-12T23:41:00.482Z
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def make_reading(temp_c: float, humidity: float) -> dict:
    #The envelope columns are top-level; the measured values go in `payload`.
    #A future sensor only changes what's inside payload (ADR 0008).
    return {
        "ts": utc_ms(),
        "sensor_id": SENSOR_ID,
        "sensor_type": SENSOR_TYPE,
        "payload": {
            "temperature_c": temp_c,
            "temperature_f": round(temp_c * 9 / 5 + 32, 1),
            "humidity": humidity,
        },
    }


def main():
    #Hardware libs only exist on the Pi; fail with a clear message elsewhere
    try:
        import board
        import adafruit_dht
    except (ImportError, NotImplementedError) as e:
        raise SystemExit(
            "dht_reader needs Blinka + adafruit-circuitpython-dht on a Pi.\n"
            "  sudo apt install swig python3-dev liblgpio-dev\n"
            "  pip install adafruit-circuitpython-dht\n"
            f"(import failed: {e})"
        )

    #use_pulseio=False is the reliable mode on the Pi 5 / lgpio backend
    dht = adafruit_dht.DHT11(getattr(board, DHT_PIN), use_pulseio=False)

    #Own connection in this process; WAL lets it write while main.py reads/writes
    db = SQLiteHandler(DB_PATH)
    print(f"DHT reader started (sensor_id={SENSOR_ID}) -> {DB_PATH}. Ctrl-C to quit.\n")

    try:
        while True:
            try:
                temp_c = dht.temperature
                humidity = dht.humidity
                if temp_c is None or humidity is None:
                    print("  (no reading, retrying)")
                    time.sleep(READ_EVERY)
                    continue

                reading = make_reading(temp_c, humidity)
                db.insert_reading(reading)
                p = reading["payload"]
                print(f"{reading['ts']}  {p['temperature_c']}C / {p['temperature_f']}F   humidity {p['humidity']}%")

            except RuntimeError as e:
                #Normal DHT checksum miss; just retry
                print(f"  (read error: {e})")

            time.sleep(READ_EVERY)

    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        db.close()
        dht.exit()


if __name__ == "__main__":
    main()
