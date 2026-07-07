import socket
import time
import serial
from threading import Lock
from PyQt6.QtCore import QObject, pyqtSignal, QTimer


class Pace5000Backend(QObject):

    connection_status_changed = pyqtSignal(bool)
    pressure_updated = pyqtSignal(float)
    source_pressures_updated = pyqtSignal(float, float)  # (positive, negative)
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
