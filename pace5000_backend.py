import socket
import time
import serial
from threading import Lock, Event
from typing import Callable, Optional
from PyQt6.QtCore import QObject, pyqtSignal, QTimer

# Pressure/rate unit factors relative to MPa (the SCPI-native unit — the
# device is always initialized to :UNIT:PRES MPA). GPa is intentionally not
# supported here: it is never sent to the device.
PRESSURE_UNIT_TO_MPA: dict[str, float] = {"MPa": 1.0, "Bar": 0.1}
RATE_UNIT_TO_MPA_PER_MIN: dict[str, float] = {
    "MPa/min": 1.0, "Bar/min": 0.1,
    "MPa/sec": 60.0, "Bar/sec": 6.0,
}

# Hardware floor: below this the PACE5000's own slew resolution becomes
# unreliable. Enforced in the GUI regardless of which pressure/time unit the
# user is working in — see rate_to_mpa_per_sec().
MIN_SLEW_RATE_MPA_PER_SEC = 0.001


def rate_to_mpa_per_sec(value: float, unit: str) -> float:
    """Convert a slew rate given in any of RATE_UNIT_TO_MPA_PER_MIN's units to MPa/sec."""
    return value * RATE_UNIT_TO_MPA_PER_MIN.get(unit, 1.0) / 60.0


class Pace5000Backend(QObject):

    connection_status_changed = pyqtSignal(bool)
    pressure_updated = pyqtSignal(float)
    source_pressures_updated = pyqtSignal(float, float)  # (positive, negative)
    setpoint_updated = pyqtSignal(float, float)  # (target, slew_rate_per_sec) — both in the device's current pressure unit
    error_occurred = pyqtSignal(str)

    def __init__(self, connection="tcp", ip_address=None, port=5025, com_port=None, baudrate=9600):
        super().__init__()
        if connection not in ("tcp", "serial"):
            raise ValueError(f"connection must be 'tcp' or 'serial', got {connection!r}")
        self.connection = connection
        # TCP params
        self.ip_address = ip_address
        self.port = port
        # Serial params
        self.com_port = com_port
        self.baudrate = baudrate

        self.sock = None    # used when connection == "tcp"
        self.ser = None     # used when connection == "serial"
        self.connected = False
        self.lock = Lock()
        self.timer = QTimer()
        self.timer.timeout.connect(self.poll_pressure)

    @property
    def _is_connected(self):
        return self.connected

    def connect_device(self):
        if self.connected:
            return
        try:
            if self.connection == "tcp":
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(5.0)
                self.sock.connect((self.ip_address, self.port))
                self.sock.settimeout(2.0)
            else:
                self.ser = serial.Serial(
                    port=self.com_port,
                    baudrate=self.baudrate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=2.0,
                )
            self.connected = True
            self.initialize_device()
            self.connection_status_changed.emit(True)
            self.timer.start(500)
            print(f"[PACE5000] connected ({self.connection})")
        except Exception as e:
            self.connected = False
            self.error_occurred.emit(f"Connection failed: {e}")

    def disconnect_device(self):
        self.timer.stop()
        self.connected = False
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass
        self.sock = None
        self.ser = None
        self.connection_status_changed.emit(False)
        print("[PACE5000] disconnected")

    def stop(self):
        self.disconnect_device()

    def initialize_device(self):
        try:
            self.write(":UNIT:PRES MPA")
            time.sleep(0.05)
            self.write(":SOUR:PRES:SLEW:MODE LINEAR")
            time.sleep(0.05)
            self.write(":SOUR:PRES:SLEW 0.020000")
            time.sleep(0.05)
            self.write("*CLS")
            time.sleep(0.05)
            print("[PACE5000] initialized")
        except Exception as e:
            self.error_occurred.emit(f"Initialization failed: {e}")

    def _send(self, cmd: str):
        encoded = (cmd + "\n").encode("ascii")
        if self.connection == "tcp":
            self.sock.sendall(encoded)
        else:
            self.ser.write(encoded)

    def _recv_line(self) -> str:
        if self.connection == "tcp":
            data = b""
            while not data.endswith(b"\n"):
                chunk = self.sock.recv(1024)
                if not chunk:
                    raise ConnectionError("Connection closed by remote host")
                data += chunk
            return data.decode("ascii").strip()
        else:
            line = self.ser.readline()
            return line.decode("ascii").strip()

    def write(self, cmd):
        if not self.connected:
            return
        with self.lock:
            try:
                self._send(cmd)
            except Exception as e:
                self.error_occurred.emit(f"Write error: {e}")
                self.disconnect_device()

    def query(self, cmd):
        if not self.connected:
            return None
        with self.lock:
            try:
                self._send(cmd)
                resp = self._recv_line()
                # Device echoes the command header before the value, e.g. ':SENS:PRES 0.009'
                return resp.split()[-1] if resp else resp
            except socket.timeout:
                self.error_occurred.emit("Query timeout")
                return None
            except Exception as e:
                self.error_occurred.emit(f"Query error: {e}")
                self.disconnect_device()
                return None

    def set_slew_rate(self, value, unit="MPa/min"):
        if unit in ("MPa/min", "Bar/min"):
            value_per_sec = value / 60.0
        else:
            value_per_sec = value
        self.write(f":SOUR:PRES:SLEW {value_per_sec:.6f}")
        time.sleep(0.05)
        resp = self.query(":SOUR:PRES:SLEW?")
        print(f"[PACE5000] SLEW = {resp} (sent {value} {unit})")

    def get_slew_rate(self):
        return self.query(":SOUR:PRES:SLEW?")

    def set_target_pressure(self, value):
        self.write(f":SOUR:PRES {value}")
        time.sleep(0.05)
        resp = self.query(":SOUR:PRES?")
        print("[PACE5000] TARGET =", resp)

    def get_target_pressure(self):
        return self.query(":SOUR:PRES?")

    def get_pressure(self):
        resp = self.query(":SENS:PRES?")
        if resp is None:
            return None
        try:
            return float(resp)
        except Exception:
            return None

    def set_control_mode(self, enabled: bool):
        val = 1 if enabled else 0
        self.write(f":OUTP:STAT {val}")
        time.sleep(0.05)
        resp = self.query(":OUTP:STAT?")
        print("[PACE5000] OUTPUT =", resp)

    def set_output_state(self, state):
        self.set_control_mode(state)

    def get_output_state(self):
        return self.query(":OUTP:STAT?")

    def get_system_error(self):
        return self.query(":SYST:ERR?")

    def get_positive_source_pressure(self):
        resp = self.query(":SOUR:PRES:COMP1?")
        if resp is None:
            return None
        try:
            return float(resp)
        except Exception:
            return None

    def get_negative_source_pressure(self):
        resp = self.query(":SOUR:PRES:COMP2?")
        if resp is None:
            return None
        try:
            return float(resp)
        except Exception:
            return None

    def poll_pressure(self):
        if not self.connected:
            return
        pressure = self.get_pressure()
        if pressure is not None:
            self.pressure_updated.emit(pressure)
        pos = self.get_positive_source_pressure()
        neg = self.get_negative_source_pressure()
        if pos is not None and neg is not None:
            self.source_pressures_updated.emit(pos, neg)
        target_raw = self.get_target_pressure()
        slew_raw = self.get_slew_rate()
        if target_raw is not None and slew_raw is not None:
            try:
                self.setpoint_updated.emit(float(target_raw), float(slew_raw))
            except ValueError:
                pass

    # ── High-level, thread-safe operations ──────────────────────────────
    #
    # These are safe to call from any thread (QThread, a plain
    # threading.Thread, or an HTTP request-handler thread): all device I/O
    # goes through write()/query(), which serialize on self.lock. They do
    # NOT depend on a running Qt event loop. They are the single, shared
    # implementation of "change pressure" / "wait for pressure reached",
    # used by apps/exp_scheduler, this app's own Scheduled Control feature,
    # and (if enabled) the HTTP API — do not re-implement this logic
    # elsewhere; call these instead.

    def set_pressure_with_ramp(
        self,
        pressure_mpa: float,
        rate_mpa_per_min: float,
        check_source_pressure: bool = True,
        slew_verify_retries: int = 3,
        on_slew_send: Optional[Callable[[], None]] = None,
        on_slew_verified: Optional[Callable[[float], None]] = None,
    ) -> None:
        """Set the target pressure, ramping at rate_mpa_per_min.

        Critical ordering: the slew rate is sent and read back to confirm the
        device actually applied it *before* the setpoint is sent. Sending the
        setpoint first (or without verifying the rate) risks the device
        applying the new setpoint at whatever rate was previously in effect.

        Raises RuntimeError if the target exceeds the +ve source pressure
        (when check_source_pressure is True), or if the slew rate cannot be
        verified after slew_verify_retries consecutive attempts.
        """
        self.write(":UNIT:PRES MPA")

        if check_source_pressure:
            pos_source = self.get_positive_source_pressure()
            if pos_source is not None and pressure_mpa > pos_source:
                raise RuntimeError(
                    f"Set pressure {pressure_mpa:.4g} MPa exceeds +ve source pressure "
                    f"({pos_source:.4g} MPa). Aborting — increase source pressure first."
                )

        if on_slew_send is not None:
            on_slew_send()
        self.set_slew_rate(rate_mpa_per_min, unit="MPa/min")

        expected_mpa_per_sec = rate_mpa_per_min / 60.0
        consecutive_failures = 0
        while True:
            actual_slew_str = self.get_slew_rate()
            if actual_slew_str is not None:
                actual_mpa_per_sec = float(actual_slew_str)
                if abs(actual_mpa_per_sec - expected_mpa_per_sec) <= 1e-5:
                    if on_slew_verified is not None:
                        on_slew_verified(actual_mpa_per_sec)
                    break
                consecutive_failures += 1
                if consecutive_failures >= slew_verify_retries:
                    raise RuntimeError(
                        f"PACE5000 slew rate verification failed "
                        f"({slew_verify_retries} consecutive): "
                        f"sent {rate_mpa_per_min:.6f} MPa/min "
                        f"({expected_mpa_per_sec:.6f} MPa/s), "
                        f"device reports {actual_mpa_per_sec:.6f} MPa/s"
                    )
            else:
                consecutive_failures += 1
                if consecutive_failures >= slew_verify_retries:
                    raise RuntimeError(
                        f"PACE5000 slew rate verification failed "
                        f"({slew_verify_retries} consecutive): no response from device"
                    )
            time.sleep(0.2)

        self.set_target_pressure(pressure_mpa)

    def wait_for_pressure(
        self,
        tol_mpa: float,
        stop_event: Optional[Event] = None,
        timeout_s: Optional[float] = None,
        poll_interval_s: float = 0.2,
        on_update: Optional[Callable[[float, float], None]] = None,
    ) -> Optional[float]:
        """Block until the measured pressure is within tol_mpa of the
        device's current target setpoint (read via get_target_pressure()).

        Returns the final measured pressure on success. Returns None if
        stop_event is set before the target is reached (cooperative
        cancellation — callers translate this into their own
        cancellation/abort signalling; this module intentionally raises no
        caller-specific exception type). Raises TimeoutError if timeout_s
        elapses first, or RuntimeError if the target pressure cannot be read.
        """
        self.write(":UNIT:PRES MPA")
        raw = self.get_target_pressure()
        if raw is None:
            raise RuntimeError("Cannot read PACE5000 target pressure")
        target_mpa = float(raw)

        deadline = time.monotonic() + timeout_s if timeout_s is not None else None
        while True:
            if stop_event is not None and stop_event.is_set():
                return None
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(
                    f"Timed out waiting for pressure to reach "
                    f"{target_mpa:.3f} MPa ± {tol_mpa:.4f} MPa"
                )
            current = self.get_pressure()
            if current is not None:
                if on_update is not None:
                    on_update(current, target_mpa)
                if abs(current - target_mpa) <= tol_mpa:
                    return current
            time.sleep(poll_interval_s)
