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

    # :SOUR:PRES:INL:TIME default, seconds (valid range 2-999) — how long the
    # reading must stay continuously inside the :SOUR:PRES:INL band before
    # wait_for_pressure() considers the pressure genuinely stable, rather
    # than reacting to a single momentary in-tolerance sample. Fixed, not a
    # user-facing toggle — same precedent as :SOUR:PRES:SLEW:OVER 0.
    DEFAULT_STABILITY_DWELL_S = 2

    connection_status_changed = pyqtSignal(bool)
    pressure_updated = pyqtSignal(float)
    source_pressures_updated = pyqtSignal(float, float)  # (positive, negative)
    setpoint_updated = pyqtSignal(float, float)  # (target, slew_rate_per_sec) — both in the device's current pressure unit
    effort_updated = pyqtSignal(float)  # :SOUR:PRES:EFF? — controller effort, -100..+100 %
    error_occurred = pyqtSignal(str)
    instrument_error_reported = pyqtSignal(int, str)  # (code, message) — device-side :SYST:ERR? entry, not a comms failure

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
        # Cached :INST:SENS1:FULL? value, used to convert an absolute
        # tol_mpa into a %FS band for :SOUR:PRES:INL. Reset on disconnect in
        # case a later reconnect targets a different range/instrument.
        self._control_fs_mpa: Optional[float] = None

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
        self._control_fs_mpa = None
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
            # Manual default is 1 (overshoot allowed). This app has no
            # scenario where overshoot near the setpoint is acceptable — a
            # DAC pressure overshoot risks uneven gasket deformation or
            # unintended overpressure — so always force it off, with no
            # user-facing toggle.
            self.write(":SOUR:PRES:SLEW:OVER 0")
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

    def _query_line(self, cmd: str) -> Optional[str]:
        """Send cmd and return the full raw response line, or None on failure.

        Shared by query() (single-value) and get_system_error() (multi-field)
        — the two callers differ only in how they trim the device's echoed
        command header off the front of the response.
        """
        if not self.connected:
            return None
        with self.lock:
            try:
                self._send(cmd)
                return self._recv_line()
            except socket.timeout:
                self.error_occurred.emit("Query timeout")
                return None
            except Exception as e:
                self.error_occurred.emit(f"Query error: {e}")
                self.disconnect_device()
                return None

    def query(self, cmd):
        resp = self._query_line(cmd)
        # Device echoes the command header before the value, e.g. ':SENS:PRES 0.009'
        return resp.split()[-1] if resp else resp

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

    def get_pressure_in_limits(self) -> Optional[tuple[float, bool]]:
        """Query :SENS:PRES:INL? — (current pressure, stably in-limits).

        Requires :SOUR:PRES:INL (band) and :SOUR:PRES:INL:TIME (dwell) to
        already be configured — the device only reports True once the
        reading has stayed within the band continuously for the configured
        dwell time, so a single True read is sufficient evidence of genuine
        stability (no client-side accumulation needed). Raw-line parsing,
        same reasoning as get_system_error(): this is a comma-separated
        multi-field response, not a single value.
        """
        resp = self._query_line(":SENS:PRES:INL?")
        if not resp:
            return None
        header, sep, body = resp.partition(" ")
        if not sep:
            body = resp
        value_str, _, flag_str = body.partition(",")
        try:
            value = float(value_str.strip())
            in_limits = bool(int(flag_str.strip()))
        except ValueError:
            return None
        return value, in_limits

    def get_control_full_scale_mpa(self) -> Optional[float]:
        """Cached :INST:SENS1:FULL? — control sensor full scale, in MPa.

        Forces :UNIT:PRES MPA before the (one-time, cached) query itself,
        rather than relying on a caller having already done so — this is a
        public method with more than one call site (wait_for_pressure() and
        the UI's initial post-connect fetch), so it must not depend on
        caller ordering to return a value in the unit its name promises.
        """
        if self._control_fs_mpa is None:
            self.write(":UNIT:PRES MPA")
            resp = self.query(":INST:SENS1:FULL?")
            if resp is None:
                return None
            try:
                self._control_fs_mpa = float(resp)
            except ValueError:
                return None
        return self._control_fs_mpa

    def get_system_error(self) -> Optional[tuple[int, str]]:
        """Pop one entry off the device's error queue.

        Unlike query(), this cannot use the last-token trim (":SENS:PRES
        0.009" -> "0.009") — :SYST:ERR? returns a comma-separated,
        possibly-quoted, possibly-multi-word field such as
        '-222,"Data out of range"', so the value has to be parsed out of
        the full line instead. Returns (0, "No error") when the queue is
        empty, or None if the query itself failed.
        """
        resp = self._query_line(":SYST:ERR?")
        if not resp:
            return None
        # Device echoes the command header before the value, e.g.
        # ':SYST:ERR -222,"Data out of range"'.
        header, sep, body = resp.partition(" ")
        if not sep:
            body = resp
        code_str, _, message = body.partition(",")
        try:
            code = int(code_str.strip())
        except ValueError:
            return None
        return code, message.strip().strip('"')

    def drain_system_errors(self, max_errors: int = 5) -> None:
        """Read and emit every error currently queued on the device.

        The PACE5000's error queue holds at most 5 entries (see
        docs/USEFUL_COMMAMDS.md), so max_errors=5 both bounds this loop and
        matches the hardware — it can never need more reads to empty the
        queue in one go. Called once per poll_pressure() tick; when the
        queue is empty (the common case) this costs a single query.
        """
        for _ in range(max_errors):
            result = self.get_system_error()
            if result is None:
                return
            code, message = result
            if code == 0:
                return
            self.instrument_error_reported.emit(code, message)

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

    def get_effort(self) -> Optional[float]:
        """:SOUR:PRES:EFF? — controller effort, -100 (vacuum valve) to
        +100 (supply valve) %. 0.0 when the controller output is off."""
        resp = self.query(":SOUR:PRES:EFF?")
        if resp is None:
            return None
        try:
            return float(resp)
        except Exception:
            return None

    def poll_pressure(self):
        if not self.connected:
            return
        self.drain_system_errors()
        pressure = self.get_pressure()
        if pressure is not None:
            self.pressure_updated.emit(pressure)
        pos = self.get_positive_source_pressure()
        neg = self.get_negative_source_pressure()
        if pos is not None and neg is not None:
            self.source_pressures_updated.emit(pos, neg)
        effort = self.get_effort()
        if effort is not None:
            self.effort_updated.emit(effort)
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
        pressure: float,
        rate_per_min: float,
        unit: str = "MPa",
        check_source_pressure: bool = True,
        slew_verify_retries: int = 3,
        on_slew_send: Optional[Callable[[], None]] = None,
        on_slew_verified: Optional[Callable[[float], None]] = None,
    ) -> None:
        """Set the target pressure, ramping at rate_per_min, both expressed
        in `unit` ("MPa" or "Bar" — a PRESSURE_UNIT_TO_MPA key).

        This forces the device's active pressure unit (:UNIT:PRES) to `unit`
        before sending either value — callers must not assume the device's
        previously active unit survives this call. Existing MPa-canonical
        callers (Scheduled Control, exp_scheduler, the HTTP API) already
        convert to MPa themselves and rely on the "MPa" default; the manual
        control tab instead passes the operator's currently selected display
        unit directly, since it keeps the device's active unit in sync with
        that selection (see Pace5000Window._on_target_pressure_unit_changed).

        Critical ordering: the slew rate is sent and read back to confirm the
        device actually applied it *before* the setpoint is sent. Sending the
        setpoint first (or without verifying the rate) risks the device
        applying the new setpoint at whatever rate was previously in effect.

        Raises RuntimeError if the target exceeds the +ve source pressure
        (when check_source_pressure is True), or if the slew rate cannot be
        verified after slew_verify_retries consecutive attempts.
        """
        scpi_unit = "MPA" if unit == "MPa" else "BAR"
        self.write(f":UNIT:PRES {scpi_unit}")

        if check_source_pressure:
            pos_source = self.get_positive_source_pressure()
            if pos_source is not None and pressure > pos_source:
                raise RuntimeError(
                    f"Set pressure {pressure:.4g} {unit} exceeds +ve source pressure "
                    f"({pos_source:.4g} {unit}). Aborting — increase source pressure first."
                )

        if on_slew_send is not None:
            on_slew_send()
        self.set_slew_rate(rate_per_min, unit=f"{unit}/min")

        expected_per_sec = rate_per_min / 60.0
        consecutive_failures = 0
        while True:
            actual_slew_str = self.get_slew_rate()
            if actual_slew_str is not None:
                actual_per_sec = float(actual_slew_str)
                if abs(actual_per_sec - expected_per_sec) <= 1e-5:
                    if on_slew_verified is not None:
                        on_slew_verified(actual_per_sec)
                    break
                consecutive_failures += 1
                if consecutive_failures >= slew_verify_retries:
                    raise RuntimeError(
                        f"PACE5000 slew rate verification failed "
                        f"({slew_verify_retries} consecutive): "
                        f"sent {rate_per_min:.6f} {unit}/min "
                        f"({expected_per_sec:.6f} {unit}/s), "
                        f"device reports {actual_per_sec:.6f} {unit}/s"
                    )
            else:
                consecutive_failures += 1
                if consecutive_failures >= slew_verify_retries:
                    raise RuntimeError(
                        f"PACE5000 slew rate verification failed "
                        f"({slew_verify_retries} consecutive): no response from device"
                    )
            time.sleep(0.2)

        self.set_target_pressure(pressure)

    def wait_for_pressure(
        self,
        tol_mpa: float,
        dwell_s: int = DEFAULT_STABILITY_DWELL_S,
        stop_event: Optional[Event] = None,
        timeout_s: Optional[float] = None,
        poll_interval_s: float = 0.2,
        on_update: Optional[Callable[[float, float], None]] = None,
    ) -> Optional[float]:
        """Block until the measured pressure has stayed within tol_mpa of the
        device's current target setpoint (read via get_target_pressure())
        continuously for dwell_s seconds.

        Stability is judged by the device itself, not a single client-side
        sample: tol_mpa is converted to a %FS band and sent as
        :SOUR:PRES:INL, dwell_s as :SOUR:PRES:INL:TIME, and each poll reads
        :SENS:PRES:INL? (see get_pressure_in_limits()) — the device only
        reports "in limits" once the reading has been continuously inside
        the band for the full dwell time, so a single such report is
        already sufficient evidence of genuine stability. This avoids
        declaring "reached" on a momentary noise spike, which a naive
        abs(current - target) <= tol single-sample check would do.

        Returns the final measured pressure on success. Returns None if
        stop_event is set before the target is reached (cooperative
        cancellation — callers translate this into their own
        cancellation/abort signalling; this module intentionally raises no
        caller-specific exception type). Raises TimeoutError if timeout_s
        elapses first, or RuntimeError if the target pressure or control
        sensor full scale cannot be read.
        """
        self.write(":UNIT:PRES MPA")
        raw = self.get_target_pressure()
        if raw is None:
            raise RuntimeError("Cannot read PACE5000 target pressure")
        target_mpa = float(raw)

        full_scale_mpa = self.get_control_full_scale_mpa()
        if full_scale_mpa is None or full_scale_mpa <= 0:
            raise RuntimeError("Cannot read PACE5000 control sensor full scale")
        band_pct = max(0.0, min(100.0, tol_mpa / full_scale_mpa * 100.0))
        dwell_s_clamped = max(2, min(999, int(round(dwell_s))))

        self.write(f":SOUR:PRES:INL {band_pct:.6f}")
        time.sleep(0.05)
        self.write(f":SOUR:PRES:INL:TIME {dwell_s_clamped}")
        time.sleep(0.05)
        # Surface a rejected band/dwell value (e.g. out of range) right away
        # instead of silently polling until timeout.
        self.drain_system_errors()

        deadline = time.monotonic() + timeout_s if timeout_s is not None else None
        while True:
            if stop_event is not None and stop_event.is_set():
                return None
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(
                    f"Timed out waiting for pressure to reach "
                    f"{target_mpa:.3f} MPa ± {tol_mpa:.4f} MPa "
                    f"(stable for {dwell_s_clamped} s)"
                )
            result = self.get_pressure_in_limits()
            if result is not None:
                current, in_limits = result
                if on_update is not None:
                    on_update(current, target_mpa)
                if in_limits:
                    return current
            time.sleep(poll_interval_s)
