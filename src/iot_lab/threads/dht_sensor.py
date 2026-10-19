import sqlite3
import threading

from iot_lab.sqlite_handler import SQLiteHandler
from iot_lab.timestamps import utc_timestamp

SENSOR_TYPE = "dht11"   #stored with every reading; tells sensor kinds apart


def make_reading(sensor_id: str, temperature_c: float, humidity: float) -> dict:
    #Envelope columns at the top level, measured values in `payload`; a future
    #sensor only changes what goes in payload (docs/adr/0009-sensor-readings.md).
    #Only measured values are stored: Fahrenheit etc. are derived when read.
    return {
        "ts": utc_timestamp(),
        "sensor_id": sensor_id,
        "sensor_type": SENSOR_TYPE,
        "payload": {"temperature_c": temperature_c, "humidity": humidity},
    }


class DHTSensor:
    """
    Producer thread: reads a DHT11 temperature/humidity sensor every
    read_every_s seconds and writes each reading to the `readings` table.

    Unlike detections, readings skip the queue + writer: the queue exists so
    inference never waits on disk, but this loop sleeps seconds between reads,
    so a ~1 ms insert costs it nothing. It opens its own SQLiteHandler (WAL
    allows it alongside the event writer) and stops on stop_event like the
    other producers.

    Wiring (3-pin module -> Pi):  VCC->3.3V(pin1)  DATA->GPIO4(pin7)  GND->GND(pin6)
    Bare 4-pin sensor: add a 10k pull-up between DATA and VCC.
    """

    def __init__(self, stop_event: threading.Event, db_path: str, sensor_id = "T1",
                 pin = "D4", read_every_s = 3.0, inserted: threading.Event = None, device = None):
        self.stop_event = stop_event
        self.db_path = db_path
        self.sensor_id = sensor_id
        #DHT11 needs >= ~2 s between reads
        self.read_every_s = read_every_s

        #Optional: set after every insert to wake the AWS forwarder
        self.inserted = inserted

        #Opened here, on the main thread, so a missing library or bad pin fails
        #at startup (fail fast on config). `device` lets tests pass a fake.
        self.device = device if device is not None else self._open_device(pin)

    @staticmethod
    def _open_device(pin):
        #Hardware libraries exist only on the Pi, so import them only when enabled
        try:
            import board
            import adafruit_dht
        except (ImportError, NotImplementedError) as e:
            raise RuntimeError(
                "ENABLE_DHT is True but the DHT libraries aren't available (Pi only):\n"
                "  sudo apt install swig python3-dev liblgpio-dev\n"
                "  pip install -e \".[pi]\"\n"
                f"(import failed: {e})"
            ) from e

        #pin is a Blinka board pin name, e.g. "D4" = GPIO4 (physical pin 7).
        #use_pulseio=False is the reliable mode on the Pi 5 / lgpio backend
        return adafruit_dht.DHT11(getattr(board, pin), use_pulseio=False)

    def task_sensor(self):
        #Own connection, opened in this thread (see SQLiteHandler docstring)
        try:
            db = SQLiteHandler(self.db_path)
        except Exception as e:
            print(f"[DHT] could not open DB, sensor disabled: {e}")
            self.device.exit()
            return

        try:
            while not self.stop_event.is_set():
                reading = self._read()
                if reading is not None:
                    try:
                        db.insert_reading(reading)
                    except sqlite3.Error as e:
                        print(f"[DHT] dropped reading {reading}: {e}")
                    else:
                        if self.inserted is not None:
                            self.inserted.set()

                #Sleep between reads, but wake at once on shutdown
                self.stop_event.wait(self.read_every_s)
        finally:
            db.close()
            self.device.exit()

    def _read(self):
        #A reading dict, or None if this attempt failed (retried next interval)
        try:
            temperature_c = self.device.temperature
            humidity = self.device.humidity
        except RuntimeError as e:
            #Normal for a DHT11: missed timing or a checksum error
            print(f"[DHT] read error, retrying: {e}")
            return None

        if temperature_c is None or humidity is None:
            print("[DHT] no reading, retrying")
            return None

        reading = make_reading(self.sensor_id, temperature_c, humidity)
        print(f"[DHT] {reading['ts']}  {temperature_c}C / {temperature_c * 9 / 5 + 32:.1f}F  humidity {humidity}%")
        return reading
