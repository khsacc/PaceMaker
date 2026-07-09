from __future__ import annotations

import os
import sys
import csv
import json
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import QApplication, QMainWindow, QMessageBox, QFileDialog, QWidget, QVBoxLayout
from PyQt6.QtCore import QObject, QTimer, pyqtSignal, Qt
import pyqtgraph as pg

try:
    from .pace5000_ui_main import PaceUI
    from .pace5000_backend import Pace5000Backend
except ImportError:
    # Standalone execution (no parent package) — make this directory's own
    # modules importable by their bare names.
    _dir = os.path.dirname(os.path.abspath(__file__))
    if _dir not in sys.path:
        sys.path.insert(0, _dir)
    from pace5000_ui_main import PaceUI
    from pace5000_backend import Pace5000Backend


_SETTINGS_PATH = Path(__file__).parent / "__localdata" / "pace5000_settings.json"
_DEFAULT_SETTINGS = {
    "connection_type": 0,
    "ip_address": "192.168.1.100",
    "port": "5025",
    "com_port": "COM1",
    "baudrate": "9600",
    "last_log_dir": "",
}


def _load_settings() -> dict:
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {**_DEFAULT_SETTINGS, **data}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULT_SETTINGS)


def _save_settings(settings: dict) -> None:
    try:
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[PACE5000] Failed to save settings: {e}")


def _save_last_log_dir(directory: str) -> None:
    s = _load_settings()
    s["last_log_dir"] = directory
    _save_settings(s)


# ==============================================================
# Scheduled Control — Live Plot Window
# ==============================================================

class SchedulePlotWindow(QWidget):
    _MAX_POINTS = 18_000  # ≈5 h at 1 sample/s

    def __init__(self, t0: float, pressure_unit: str, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Scheduled Control — Live Pressure")
        self.resize(880, 462)
        self._t0        = t0
        self._times     = deque(maxlen=self._MAX_POINTS)
        self._pressures = deque(maxlen=self._MAX_POINTS)

        layout = QVBoxLayout(self)
        self._plot_widget = pg.PlotWidget()
        self._plot_widget.setLabel("left", f"Pressure ({pressure_unit})")
        self._plot_widget.setLabel("bottom", "Elapsed Time", units="s")
        self._plot_widget.getAxis("left").enableAutoSIPrefix(False)
        self._plot_widget.getAxis("bottom").enableAutoSIPrefix(False)
        self._plot_widget.showGrid(x=True, y=True, alpha=0.2)
        self._curve = self._plot_widget.plot(pen=pg.mkPen("#e63900", width=2))
        self._curve.setDownsampling(auto=True, method='peak')
        self._curve.setClipToView(True)
        layout.addWidget(self._plot_widget)

    def add_point(self, pressure: float):
        self._times.append(time.time() - self._t0)
        self._pressures.append(pressure)
        self._curve.setData(list(self._times), list(self._pressures))


# ==============================================================
# Scheduled Control Runner
# ==============================================================

class ScheduledControlRunner(QObject):
    status_changed   = pyqtSignal(str)
    item_activated   = pyqtSignal(int)
    pressure_warning = pyqtSignal(str)
    completed        = pyqtSignal()
    stopped          = pyqtSignal()

    _ETA_WARNING_SEC = 300
    _PRESSURE_TOL = {"MPA": 0.0001, "BAR": 0.0001}
    _UNIT_SCPI = {"MPa": "MPA", "Bar": "BAR"}

    def __init__(self, items: list, backend: Pace5000Backend, parent=None):
        super().__init__(parent)
        self.items   = items
        self.backend = backend
        self.current_index = 0
        self._running = False

        self._wait_timer = QTimer(self)
        self._wait_timer.setSingleShot(True)
        self._wait_timer.timeout.connect(self._on_wait_done)

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(200)
        self._tick_timer.timeout.connect(self._tick)

        self._wait_start    = 0.0
        self._wait_duration = 0.0

        self._latest_pressure     = None
        self._waiting_for_pressure = False
        self._target_pressure      = 0.0
        self._target_display_str   = ""
        self._target_scpi_unit     = "BAR"
        self._device_scpi_unit     = "BAR"
        self._pressure_eta         = None
        self._eta_warning_sent     = False

    def start(self):
        self._running = True
        self.current_index = 0
        self._waiting_for_pressure = False
        self._latest_pressure = None
        self.backend.pressure_updated.connect(self._on_pressure_update)
        self._tick_timer.start()
        self._execute_current()

    def stop(self):
        self._running = False
        self._wait_timer.stop()
        self._tick_timer.stop()
        self._disconnect_pressure()
        self.status_changed.emit("Status: Stopped")
        self.stopped.emit()

    def _disconnect_pressure(self):
        try:
            self.backend.pressure_updated.disconnect(self._on_pressure_update)
        except Exception:
            pass

    def _on_pressure_update(self, pressure: float):
        self._latest_pressure = pressure

    def _execute_current(self):
        if not self._running:
            return
        if self.current_index >= len(self.items):
            self._tick_timer.stop()
            self._disconnect_pressure()
            self.status_changed.emit("Status: Complete ✓")
            self.completed.emit()
            return

        item  = self.items[self.current_index]
        n     = self.current_index + 1
        total = len(self.items)
        self.item_activated.emit(self.current_index)

        if item["type"] == "wait":
            self._waiting_for_pressure = False
            self._wait_duration = item["duration"]
            self._wait_start    = time.time()
            self._wait_timer.start(int(self._wait_duration * 1000))
            self.status_changed.emit(
                f"Running [{n}/{total}]: Wait {self._wait_duration:.1f} s"
            )

        elif item["type"] == "change_pressure":
            scpi_unit    = self._UNIT_SCPI.get(item["pressure_unit"], "BAR")
            rate_per_sec = item["rate"] / 60.0 if "/min" in item["rate_unit"] else item["rate"]

            self.backend.write(f":UNIT:PRES {scpi_unit}")

            latest = self._latest_pressure
            if latest is not None and rate_per_sec > 0:
                current_in_new = self._convert_pressure(
                    latest, self._device_scpi_unit, scpi_unit
                )
                delta = abs(item["pressure"] - current_in_new)
                self._pressure_eta = time.time() + delta / rate_per_sec
            else:
                self._pressure_eta = None

            self._device_scpi_unit = scpi_unit

            self.backend.write(f":SOUR:PRES:SLEW {rate_per_sec:.6f}")
            self.backend.write(f":SOUR:PRES {item['pressure']:.6f}")

            self._target_pressure    = item["pressure"]
            self._target_display_str = f"{item['pressure']:.4g} {item['pressure_unit']}"
            self._target_scpi_unit   = scpi_unit
            self._waiting_for_pressure = True
            self._eta_warning_sent     = False

            self.status_changed.emit(
                f"Running [{n}/{total}]: Pressure → {self._target_display_str} (monitoring...)"
            )

    def _on_wait_done(self):
        self.current_index += 1
        self._execute_current()

    def _tick(self):
        if not self._running or self.current_index >= len(self.items):
            return

        item  = self.items[self.current_index]
        n     = self.current_index + 1
        total = len(self.items)

        if item["type"] == "wait" and self._wait_start > 0:
            remaining = max(0.0, self._wait_duration - (time.time() - self._wait_start))
            self.status_changed.emit(
                f"Running [{n}/{total}]: Wait — {remaining:.1f} s remaining"
            )

        elif item["type"] == "change_pressure" and self._waiting_for_pressure:
            pressure = self._latest_pressure
            if pressure is None:
                return

            target = self._target_pressure
            tol    = self._PRESSURE_TOL.get(self._target_scpi_unit, 0.02)

            if abs(pressure - target) <= tol:
                self._waiting_for_pressure = False
                self.current_index += 1
                self._execute_current()
                return

            current_disp = f"{pressure:.4g} {item['pressure_unit']}"
            warning_str  = ""
            now = time.time()
            if self._pressure_eta and now > self._pressure_eta + self._ETA_WARNING_SEC:
                delay_min = (now - self._pressure_eta) / 60.0
                warning_str = f"  ⚠ ETA exceeded by {delay_min:.1f} min"
                if not self._eta_warning_sent:
                    self._eta_warning_sent = True
                    self.pressure_warning.emit(
                        f"⚠  Pressure has not reached target {self._target_display_str} — "
                            f"{delay_min:.1f} min past the estimated arrival time.\n"
                            f"The sequence continues monitoring. Press Stop to abort."
                    )

            self.status_changed.emit(
                f"Running [{n}/{total}]: Pressure → {self._target_display_str}  (current: {current_disp}){warning_str}"
            )

    @staticmethod
    def _convert_pressure(value: float, from_scpi: str, to_scpi: str) -> float:
        if from_scpi == to_scpi:
            return value
        if from_scpi == "BAR" and to_scpi == "MPA":
            return value / 10.0
        if from_scpi == "MPA" and to_scpi == "BAR":
            return value * 10.0
        return value


# ==============================================================
# App Controller
# ==============================================================

class AppController:
    def __init__(self, view: PaceUI, backend: Pace5000Backend = None):
        self.view    = view
        self._runner = None
        self._owns_backend = backend is None

        self.time_data:     deque = deque()
        self.pressure_data: deque = deque()

        self._logging_active     = False
        self._log_file           = None
        self._log_writer         = None
        self._log_write_buffer:  list = []
        self._log_record_count   = 0
        self._log_path           = ""
        self._logging_start_time = 0.0

        self._sched_logging           = False
        self._sched_log_start         = 0.0
        self._sched_log_dir           = ""
        self._sched_log_file          = None
        self._sched_log_writer        = None
        self._sched_log_write_buffer: list = []
        self._sched_log_record_count  = 0
        self._sched_log_path          = ""

        self._schedule_items: list = []
        self._edit_index     = None
        self._logging_unit   = "MPa"
        self._sched_plot_window: SchedulePlotWindow | None = None
        self._last_target_str = ""
        self._positive_source: float | None = None
        self._negative_source: float | None = None

        if backend is not None:
            # Connection managed by the launcher — wire signals and hide connection UI
            self.backend = backend
            backend.connection_status_changed.connect(self.handle_connection_status)
            backend.pressure_updated.connect(self.update_plot)
            backend.source_pressures_updated.connect(self.update_source_pressures)
            backend.error_occurred.connect(self.handle_error)
            self.view.conn_group.setVisible(False)
            self.handle_connection_status(True)
        else:
            self.backend = None
            self._apply_settings(_load_settings())

        _init_log_dir = _load_settings().get("last_log_dir", "")
        if _init_log_dir and os.path.isdir(_init_log_dir):
            self._sched_log_dir = _init_log_dir
            self.view.sched_log_dir_display.setText(_init_log_dir)

        self.setup_connections()
        self._update_rate_units("MPa")

    # ----------------------------------------------------------
    def setup_connections(self):
        self.view.btn_connect.clicked.connect(self.connect_device)
        self.view.btn_disconnect.clicked.connect(self.disconnect_device)

        self.view.radio_control.toggled.connect(self._on_mode_radio_changed)
        self.view.radio_measure.toggled.connect(self._on_mode_radio_changed)
        self.view.target_input.returnPressed.connect(self.update_target)
        self.view.radio_unit_mpa.toggled.connect(
            lambda checked: checked and self._on_target_pressure_unit_changed("MPa")
        )
        self.view.radio_unit_bar.toggled.connect(
            lambda checked: checked and self._on_target_pressure_unit_changed("Bar")
        )
        self.view.rate_input.returnPressed.connect(self.update_rate)
        self.view.btn_rel_minus.clicked.connect(lambda: self._apply_relative_change(-1))
        self.view.btn_rel_plus.clicked.connect(lambda: self._apply_relative_change(1))
        self.view.btn_log_start.clicked.connect(self.start_logging)
        self.view.btn_log_stop.clicked.connect(self.stop_logging)

        self.view.sched_item_type.currentIndexChanged.connect(self._on_sched_type_changed)
        self.view.sched_pressure_unit.currentTextChanged.connect(self._update_rate_units)
        self.view.btn_sched_add.clicked.connect(self.sched_add_or_update_item)
        self.view.btn_sched_cancel_edit.clicked.connect(self.sched_cancel_edit)
        self.view.btn_sched_edit.clicked.connect(self.sched_edit_item)
        self.view.btn_sched_up.clicked.connect(self.sched_move_up)
        self.view.btn_sched_down.clicked.connect(self.sched_move_down)
        self.view.btn_sched_delete.clicked.connect(self.sched_delete_item)

        self.view.btn_clear_graph.clicked.connect(self.clear_graph)
        self.view.interval_spinbox.valueChanged.connect(self._on_interval_changed)

        self.view.btn_sched_save.clicked.connect(self.sched_save)
        self.view.btn_sched_load.clicked.connect(self.sched_load)
        self.view.btn_sched_browse_dir.clicked.connect(self.sched_browse_dir)
        self.view.btn_sched_start.clicked.connect(self.sched_start)
        self.view.btn_sched_stop.clicked.connect(self.sched_stop)

    def _apply_settings(self, s: dict):
        self.view.conn_type_combo.setCurrentIndex(s["connection_type"])
        self.view.ip_input.setText(s["ip_address"])
        self.view.port_input.setText(s["port"])
        self.view.com_port_input.setText(s["com_port"])
        self.view.baudrate_input.setText(s["baudrate"])

    # ----------------------------------------------------------
    def connect_device(self):
        if not self._owns_backend:
            return
        conn_idx  = self.view.conn_type_combo.currentIndex()
        conn_type = "tcp" if conn_idx == 0 else "serial"
        if conn_type == "tcp":
            ip = self.view.ip_input.text().strip()
            if not ip:
                QMessageBox.warning(self.view, "Error", "Please enter an IP Address.")
                return
            try:
                port = int(self.view.port_input.text().strip())
            except ValueError:
                QMessageBox.warning(self.view, "Error", "Port must be an integer.")
                return
            _save_settings({**_load_settings(), "connection_type": conn_idx, "ip_address": ip, "port": str(port)})
            self.backend = Pace5000Backend(connection="tcp", ip_address=ip, port=port)
        else:
            com_port = self.view.com_port_input.text().strip()
            if not com_port:
                QMessageBox.warning(self.view, "Error", "Please enter a COM port (e.g. COM1).")
                return
            try:
                baudrate = int(self.view.baudrate_input.text().strip())
            except ValueError:
                QMessageBox.warning(self.view, "Error", "Baud rate must be an integer.")
                return
            _save_settings({**_load_settings(), "connection_type": conn_idx, "com_port": com_port, "baudrate": str(baudrate)})
            self.backend = Pace5000Backend(connection="serial", com_port=com_port, baudrate=baudrate)
        self.backend.connection_status_changed.connect(self.handle_connection_status)
        self.backend.pressure_updated.connect(self.update_plot)
        self.backend.source_pressures_updated.connect(self.update_source_pressures)
        self.backend.error_occurred.connect(self.handle_error)
        self.view.status_label.setText("Status: Connecting...")
        self.backend.connect_device()

    def disconnect_device(self):
        if self._logging_active:
            self.stop_logging()
        if self._runner:
            self._runner.stop()
            self._runner = None
        if self._owns_backend and self.backend:
            self.backend.stop()
            self.backend = None

    # ----------------------------------------------------------
    def handle_connection_status(self, connected: bool):
        if connected:
            self.view.status_label.setText("Status: Connected")
            self.view.status_label.setStyleSheet("color: green; font-weight: bold;")
            self.view.btn_connect.setEnabled(False)
            self.view.btn_disconnect.setEnabled(True)
            self.view.btn_log_start.setEnabled(True)
            interval_ms = int(self.view.interval_spinbox.value() * 1000)
            self.backend.timer.setInterval(interval_ms)
            QTimer.singleShot(100, self._do_initial_fetch)
        else:
            self.view.status_label.setText("Status: Disconnected")
            self.view.status_label.setStyleSheet("color: red; font-weight: bold;")
            self.view.source_pressure_label.setText("−ve source:  ---    +ve source:  ---")
            self._positive_source = None
            self._negative_source = None
            self.view.btn_connect.setEnabled(True)
            self.view.btn_disconnect.setEnabled(False)
            self.view.btn_log_start.setEnabled(False)
            self.view.btn_log_stop.setEnabled(False)
            if self._runner:
                self._runner.stop()
                self._runner = None

    # ----------------------------------------------------------
    def clear_graph(self):
        self.time_data.clear()
        self.pressure_data.clear()
        self.view.plot_curve.setData([], [])
        self.view.plot_widget.plotItem.vb.setLimits(xMin=0, xMax=None)
        self.view.plot_widget.plotItem.enableAutoRange()

    def _on_interval_changed(self, value_s: float):
        if self.backend and self.backend.connected:
            self.backend.timer.setInterval(int(value_s * 1000))

    # ----------------------------------------------------------
    def update_plot(self, pressure: float):
        t = time.time()
        self.time_data.append(t)
        self.pressure_data.append(pressure)

        unit = self.view.get_pressure_unit()
        self.view.live_pressure_label.setText(
            f"Current Pressure:  {pressure:.4f}  {unit}"
        )

        cutoff = t - 3600
        while len(self.time_data) > 1 and self.time_data[0] < cutoff:
            self.time_data.popleft()
            self.pressure_data.popleft()

        window_sec = self.view.plot_time_window_spinbox.value()
        t0     = self.time_data[0]
        x_data = [(v - t0) for v in self.time_data]
        y_data = list(self.pressure_data)
        x_last = x_data[-1]
        x_min_display = max(0.0, x_last - window_sec)
        self.view.plot_curve.setData(x_data, y_data)
        self.view.plot_widget.setXRange(x_min_display, x_last, padding=0.05)

        visible_y = [y for x, y in zip(x_data, y_data) if x >= x_min_display]
        if visible_y:
            y_min, y_max = min(visible_y), max(visible_y)
            y_pad = (y_max - y_min) * 0.1 if y_max != y_min else abs(y_max) * 0.05 or 0.01
            self.view.plot_widget.setYRange(y_min - y_pad, y_max + y_pad, padding=0)

        if self._logging_active and self._log_writer:
            elapsed  = t - self._logging_start_time
            iso_ts   = datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S.%f")
            neg_src  = self._negative_source if self._negative_source is not None else ""
            pos_src  = self._positive_source if self._positive_source is not None else ""
            self._log_write_buffer.append((iso_ts, elapsed, pressure, neg_src, pos_src))
            self._log_record_count += 1
            if len(self._log_write_buffer) >= 10:
                self._flush_log()
            if self._log_record_count % 10 == 0:
                self.view.log_count_label.setText(f"Records: {self._log_record_count}")

        if self._sched_logging and self._sched_log_writer:
            elapsed  = t - self._sched_log_start
            iso_ts   = datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S.%f")
            neg_src  = self._negative_source if self._negative_source is not None else ""
            pos_src  = self._positive_source if self._positive_source is not None else ""
            self._sched_log_write_buffer.append((iso_ts, elapsed, pressure, neg_src, pos_src))
            self._sched_log_record_count += 1
            if len(self._sched_log_write_buffer) >= 10:
                self._flush_sched_log()
            if self._sched_log_record_count % 10 == 0:
                self.view.sched_record_label.setText(f"Records: {self._sched_log_record_count}")

    def update_source_pressures(self, positive: float, negative: float):
        self._positive_source = positive
        self._negative_source = negative
        unit = self.view.get_pressure_unit()
        self.view.source_pressure_label.setText(
            f"−ve source:  {negative:.4f}  {unit}    +ve source:  {positive:.4f}  {unit}"
        )

    # ----------------------------------------------------------
    def start_logging(self):
        if self._logging_active:
            return
        default_name = datetime.now().strftime("pace5000_log_%Y%m%d_%H%M%S.csv")
        default = (
            os.path.join(self._sched_log_dir, default_name)
            if self._sched_log_dir and os.path.isdir(self._sched_log_dir)
            else default_name
        )
        path, _ = QFileDialog.getSaveFileName(
            self.view, "Save Log Data", default,
            "CSV Files (*.csv);;All Files (*)",
        )
        if not path:
            return
        selected_dir = str(Path(path).parent)
        self._sched_log_dir = selected_dir
        self.view.sched_log_dir_display.setText(selected_dir)
        _save_last_log_dir(selected_dir)
        unit = self.view.get_pressure_unit()
        try:
            f = open(path, "w", newline="", encoding="utf-8-sig")
            writer = csv.writer(f)
            writer.writerow(["Timestamp", "Elapsed_s", f"Pressure_{unit}", f"Source_neg_{unit}", f"Source_pos_{unit}"])
            f.flush()
        except Exception as e:
            QMessageBox.critical(self.view, "File Error", f"Cannot open log file.\n{e}")
            return
        self._log_file           = f
        self._log_writer         = writer
        self._log_path           = path
        self._log_write_buffer   = []
        self._log_record_count   = 0
        self._logging_start_time = time.time()
        self._logging_active     = True
        self.view.btn_log_start.setEnabled(False)
        self.view.btn_log_stop.setEnabled(True)
        self.view.set_pressure_unit_enabled(False)
        self.view.log_status_label.setText("Log: Recording ●")
        self.view.log_status_label.setStyleSheet("color: red; font-weight: bold;")
        self.view.log_count_label.setText("Records: 0")

    def stop_logging(self):
        if not self._logging_active:
            return
        self._logging_active = False
        self._flush_log()
        count = self._log_record_count
        path  = self._log_path
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file   = None
            self._log_writer = None
        self.view.btn_log_stop.setEnabled(False)
        self.view.set_pressure_unit_enabled(True)
        if self.backend and self.backend._is_connected:
            self.view.btn_log_start.setEnabled(True)
        self.view.log_status_label.setText("Log: Stopped")
        self.view.log_status_label.setStyleSheet("color: gray; font-weight: bold;")
        QMessageBox.information(
            self.view, "Logging Stopped",
            f"Saved {count} records.\n\n{path}",
        )

    def _flush_log(self):
        if not self._log_writer or not self._log_write_buffer:
            return
        try:
            self._log_writer.writerows(self._log_write_buffer)
            self._log_file.flush()
        except Exception as e:
            QMessageBox.critical(self.view, "Write Error", f"Failed to write log.\n{e}")
        self._log_write_buffer.clear()

    def _flush_sched_log(self):
        if not self._sched_log_writer or not self._sched_log_write_buffer:
            return
        try:
            self._sched_log_writer.writerows(self._sched_log_write_buffer)
            self._sched_log_file.flush()
        except Exception as e:
            QMessageBox.critical(self.view, "Write Error", f"Failed to write schedule log.\n{e}")
        self._sched_log_write_buffer.clear()

    # ----------------------------------------------------------
    def _on_mode_radio_changed(self, checked: bool):
        if not checked:
            return
        is_control = self.view.radio_control.isChecked()
        if self.backend:
            self.backend.set_control_mode(is_control)

    def update_target(self):
        if not self.backend or not self.backend._is_connected:
            QMessageBox.warning(self.view, "Error", "Device not connected!")
            return
        try:
            val = float(self.view.target_input.text())
            unit = self.view.get_pressure_unit()
            rate_val = self.view.rate_input.text().strip()
            rate_unit = f"{self.view.rate_pressure_unit_display.text()}/{self.view.rate_time_combo.currentText()}"
        except ValueError:
            QMessageBox.warning(self.view, "Error", "Invalid target pressure. Numbers only.")
            return
        if self._positive_source is not None and val > self._positive_source:
            QMessageBox.warning(
                self.view, "Target Exceeds +ve Source",
                f"Set value ({val:.4g} {unit}) exceeds +ve source pressure "
                    f"({self._positive_source:.4g} {unit}).\n"
                    f"Target has not been updated."
            )
            self.view.target_input.setText(self._last_target_str)
            return
        if self.view.chk_confirm_before_apply.isChecked():
            msg = f"Go to {val} {unit} at {rate_val} {rate_unit}?"
            reply = QMessageBox.question(
                self.view, "Confirm", msg,
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Ok:
                return
        scpi_unit = "MPA" if unit == "MPa" else "BAR"
        self.backend.write(f":UNIT:PRES {scpi_unit}")
        self.backend.set_target_pressure(val)
        self._last_target_str = self.view.target_input.text()
        self.view.target_input.setStyleSheet("background-color: #e6ffe6;")
        QMessageBox.information(self.view, "Success", f"Target updated to: {val} {unit}")

    def update_rate(self):
        if not self.backend or not self.backend._is_connected:
            QMessageBox.warning(self.view, "Error", "Device not connected!")
            return
        try:
            val = float(self.view.rate_input.text())
            pressure_unit = self.view.get_pressure_unit()
            time_unit = self.view.rate_time_combo.currentText()
            unit = f"{pressure_unit}/{time_unit}"
            self.backend.set_slew_rate(val, unit)
            self.view.rate_input.setStyleSheet("background-color: #e6ffe6;")
            QMessageBox.information(self.view, "Success", f"Rate updated to: {val} {unit}")
        except ValueError:
            QMessageBox.warning(self.view, "Error", "Invalid rate value. Numbers only.")

    def _apply_relative_change(self, sign: int):
        if not self.backend or not self.backend._is_connected:
            QMessageBox.warning(self.view, "Error", "Device not connected!")
            return
        try:
            current = float(self.view.target_input.text())
        except ValueError:
            QMessageBox.warning(self.view, "Error", "No valid target pressure in the input field.")
            return
        try:
            step = float(self.view.rel_step_spinbox.text())
        except ValueError:
            QMessageBox.warning(self.view, "Error", "Invalid step value.")
            return
        new_val   = current + sign * step
        unit      = self.view.get_pressure_unit()
        scpi_unit = "MPA" if unit == "MPa" else "BAR"
        self.backend.write(f":UNIT:PRES {scpi_unit}")
        if self._positive_source is not None and new_val > self._positive_source:
            QMessageBox.warning(
                self.view, "Target Exceeds +ve Source",
                f"Set value ({new_val:.4g} {unit}) exceeds +ve source pressure "
                    f"({self._positive_source:.4g} {unit}).\n"
                    f"Target has not been updated."
            )
            return
        self.backend.set_target_pressure(new_val)
        self._last_target_str = f"{new_val:.4g}"
        self.view.target_input.setText(f"{new_val:.4g}")
        self.view.target_input.setStyleSheet("background-color: #e6ffe6;")

    # ----------------------------------------------------------
    def _do_initial_fetch(self):
        if not self.backend or not self.backend._is_connected:
            return
        pressure = self.backend.get_pressure()
        if pressure is not None:
            self.update_plot(pressure)
        setpoint = self.backend.get_target_pressure()
        if setpoint is not None:
            self.view.target_input.setText(setpoint)
            self._last_target_str = setpoint
        output_state = self.backend.get_output_state()
        if output_state is not None:
            is_control = output_state.strip() in ("1", "ON")
            self.view.radio_control.blockSignals(True)
            self.view.radio_measure.blockSignals(True)
            self.view.radio_control.setChecked(is_control)
            self.view.radio_measure.setChecked(not is_control)
            self.view.radio_control.blockSignals(False)
            self.view.radio_measure.blockSignals(False)
        pos = self.backend.get_positive_source_pressure()
        neg = self.backend.get_negative_source_pressure()
        if pos is not None and neg is not None:
            self.update_source_pressures(pos, neg)

    def _on_target_pressure_unit_changed(self, unit: str):
        self.view.rate_pressure_unit_display.setText(unit)
        self.view.plot_widget.setLabel("left", f"Pressure ({unit})")
        if self.backend and self.backend._is_connected:
            scpi_unit = "MPA" if unit == "MPa" else "BAR"
            self.backend.write(f":UNIT:PRES {scpi_unit}")
        old_unit = self._logging_unit
        if old_unit != unit:
            factor = 10.0 if (old_unit == "MPa" and unit == "Bar") else 0.1
            self.pressure_data = deque(p * factor for p in self.pressure_data)
            if self.pressure_data:
                self.view.plot_curve.setData(
                    [(v - self.time_data[0]) for v in self.time_data],
                    list(self.pressure_data),
                )
            self._logging_unit = unit

    def _on_sched_type_changed(self, index: int):
        self.view.sched_param_stack.setCurrentIndex(index)

    def _update_rate_units(self, pressure_unit: str):
        current = self.view.sched_rate_unit.currentText()
        suffix  = "min" if "/min" in current else "sec"
        self.view.sched_rate_unit.blockSignals(True)
        self.view.sched_rate_unit.clear()
        self.view.sched_rate_unit.addItems([f"{pressure_unit}/min", f"{pressure_unit}/sec"])
        idx = self.view.sched_rate_unit.findText(f"{pressure_unit}/{suffix}")
        self.view.sched_rate_unit.setCurrentIndex(idx if idx >= 0 else 0)
        self.view.sched_rate_unit.blockSignals(False)

    def _populate_form(self, item: dict):
        if item["type"] == "wait":
            self.view.sched_item_type.setCurrentIndex(0)
            self.view.sched_wait_duration.setText(str(item["duration"]))
        else:
            self.view.sched_item_type.setCurrentIndex(1)
            pu_idx = self.view.sched_pressure_unit.findText(item["pressure_unit"])
            if pu_idx >= 0:
                self.view.sched_pressure_unit.setCurrentIndex(pu_idx)
            self.view.sched_pressure_input.setText(str(item["pressure"]))
            ru_idx = self.view.sched_rate_unit.findText(item["rate_unit"])
            if ru_idx >= 0:
                self.view.sched_rate_unit.setCurrentIndex(ru_idx)
            self.view.sched_rate_input.setText(str(item["rate"]))

    def _clear_form(self):
        self.view.sched_wait_duration.clear()
        self.view.sched_pressure_input.clear()
        self.view.sched_rate_input.clear()

    def _enter_edit_mode(self, index: int):
        self._edit_index = index
        self._populate_form(self._schedule_items[index])
        self.view.btn_sched_add.setText(f"✎ Update Item {index + 1}")
        self.view.btn_sched_cancel_edit.setVisible(True)
        self.view.sched_list.setCurrentRow(index)

    def _exit_edit_mode(self):
        self._edit_index = None
        self._clear_form()
        self.view.btn_sched_add.setText("＋ Add to Schedule")
        self.view.btn_sched_cancel_edit.setVisible(False)

    def sched_edit_item(self):
        row = self.view.sched_list.currentRow()
        if 0 <= row < len(self._schedule_items):
            self._enter_edit_mode(row)

    def sched_cancel_edit(self):
        self._exit_edit_mode()

    def sched_add_or_update_item(self):
        is_wait = self.view.sched_item_type.currentIndex() == 0
        if is_wait:
            try:
                duration = float(self.view.sched_wait_duration.text())
                if duration <= 0:
                    raise ValueError
            except ValueError:
                QMessageBox.warning(self.view, "Error", "Please enter a positive number of seconds.")
                return
            new_item = {"type": "wait", "duration": duration}
        else:
            try:
                pressure = float(self.view.sched_pressure_input.text())
                rate     = float(self.view.sched_rate_input.text())
                if rate <= 0:
                    raise ValueError
            except ValueError:
                QMessageBox.warning(self.view, "Error", "Please enter valid numbers for pressure and rate.")
                return
            new_item = {
                "type":          "change_pressure",
                "pressure":      pressure,
                "pressure_unit": self.view.sched_pressure_unit.currentText(),
                "rate":          rate,
                "rate_unit":     self.view.sched_rate_unit.currentText(),
            }

        if self._edit_index is not None:
            self._schedule_items[self._edit_index] = new_item
            self._exit_edit_mode()
        else:
            self._schedule_items.append(new_item)
            self._clear_form()
        self._refresh_sched_list()

    def sched_delete_item(self):
        row = self.view.sched_list.currentRow()
        if 0 <= row < len(self._schedule_items):
            self._schedule_items.pop(row)
            if self._edit_index is not None:
                if self._edit_index == row:
                    self._exit_edit_mode()
                elif self._edit_index > row:
                    self._edit_index -= 1
                    self.view.btn_sched_add.setText(f"✎ Update Item {self._edit_index + 1}")
            self._refresh_sched_list()

    def sched_move_up(self):
        row = self.view.sched_list.currentRow()
        if row > 0:
            items = self._schedule_items
            items[row - 1], items[row] = items[row], items[row - 1]
            if self._edit_index == row:
                self._edit_index = row - 1
            elif self._edit_index == row - 1:
                self._edit_index = row
            self._refresh_sched_list()
            self.view.sched_list.setCurrentRow(row - 1)

    def sched_move_down(self):
        row = self.view.sched_list.currentRow()
        if 0 <= row < len(self._schedule_items) - 1:
            items = self._schedule_items
            items[row], items[row + 1] = items[row + 1], items[row]
            if self._edit_index == row:
                self._edit_index = row + 1
            elif self._edit_index == row + 1:
                self._edit_index = row
            self._refresh_sched_list()
            self.view.sched_list.setCurrentRow(row + 1)

    def _refresh_sched_list(self):
        current_row = self.view.sched_list.currentRow()
        self.view.sched_list.clear()
        for i, item in enumerate(self._schedule_items):
            self.view.sched_list.addItem(self._format_item(i, item))
        if 0 <= current_row < self.view.sched_list.count():
            self.view.sched_list.setCurrentRow(current_row)

    @staticmethod
    def _format_item(i: int, item: dict) -> str:
        n = i + 1
        if item["type"] == "wait":
            return f'{n}.  Wait — {item["duration"]:.1f} s'
        return f'{n}.  Change Pressure — {item["pressure"]:.4g} {item["pressure_unit"]}  @  {item["rate"]:.4g} {item["rate_unit"]}'

    def sched_save(self):
        if not self._schedule_items:
            QMessageBox.warning(self.view, "Save Schedule", "Schedule is empty.")
            return
        default = datetime.now().strftime("schedule_%Y%m%d_%H%M%S.json")
        path, _ = QFileDialog.getSaveFileName(
            self.view, "Save Schedule", default,
            "PACE Schedule (*.json);;All Files (*)",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "items": self._schedule_items}, f, indent=2, ensure_ascii=False)
        except Exception as e:
            QMessageBox.critical(self.view, "Save Error", f"Failed to save schedule.\n{e}")

    def sched_load(self):
        if self._runner:
            QMessageBox.warning(self.view, "Load Schedule", "Cannot load while a schedule is running.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self.view, "Load Schedule", "",
            "PACE Schedule (*.json);;All Files (*)",
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw = data["items"] if isinstance(data, dict) else data
            if not isinstance(raw, list):
                raise ValueError("Top-level value must be a list of items.")
            validated = []
            for item in raw:
                t = item.get("type")
                if t == "wait":
                    validated.append({"type": "wait", "duration": float(item["duration"])})
                elif t == "change_pressure":
                    validated.append({
                        "type":          "change_pressure",
                        "pressure":      float(item["pressure"]),
                        "pressure_unit": str(item["pressure_unit"]),
                        "rate":          float(item["rate"]),
                        "rate_unit":     str(item["rate_unit"]),
                    })
                else:
                    raise ValueError(f"Unknown item type: {t!r}")
            self._schedule_items = validated
            self._exit_edit_mode()
            self._refresh_sched_list()
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            QMessageBox.critical(self.view, "Load Error", f"Failed to load schedule.\n{e}")

    # ----------------------------------------------------------
    def sched_browse_dir(self):
        directory = QFileDialog.getExistingDirectory(
            self.view, "Select Log Save Folder", self._sched_log_dir or ""
        )
        if directory:
            self._sched_log_dir = directory
            self.view.sched_log_dir_display.setText(directory)
            _save_last_log_dir(directory)

    def sched_start(self):
        if not self.backend or not self.backend._is_connected:
            QMessageBox.warning(self.view, "Error", "Device is not connected.")
            return
        if not self._schedule_items:
            QMessageBox.warning(self.view, "Error", "Schedule is empty.")
            return

        ts           = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"pace5000_schedule_{ts}.csv"
        default_path = (
            os.path.join(self._sched_log_dir, default_name)
            if self._sched_log_dir and os.path.isdir(self._sched_log_dir)
            else default_name
        )
        path, _ = QFileDialog.getSaveFileName(
            self.view, "Save Schedule Log", default_path,
            "CSV Files (*.csv);;All Files (*)",
        )
        if not path:
            return
        selected_dir = str(Path(path).parent)
        self._sched_log_dir = selected_dir
        self.view.sched_log_dir_display.setText(selected_dir)
        _save_last_log_dir(selected_dir)

        unit = self.view.get_pressure_unit()
        try:
            f = open(path, "w", newline="", encoding="utf-8-sig")
            writer = csv.writer(f)
            writer.writerow(["Timestamp", "Elapsed_s", f"Pressure_{unit}", f"Source_neg_{unit}", f"Source_pos_{unit}"])
            f.flush()
        except Exception as e:
            QMessageBox.critical(self.view, "File Error", f"Cannot open log file.\n{e}")
            return

        self._sched_log_file         = f
        self._sched_log_writer       = writer
        self._sched_log_path         = path
        self._sched_log_write_buffer = []
        self._sched_log_record_count = 0
        self._sched_log_start        = time.time()
        self._sched_logging          = True
        self.view.sched_record_label.setText("Records: 0")
        self.view.sched_warning_label.setVisible(False)
        self.view.set_pressure_unit_enabled(False)

        self._runner = ScheduledControlRunner(list(self._schedule_items), self.backend)
        self._runner.status_changed.connect(self._on_sched_status)
        self._runner.item_activated.connect(self.view.sched_list.setCurrentRow)
        self._runner.pressure_warning.connect(self._on_sched_warning)
        self._runner.completed.connect(self._on_sched_completed)
        self._runner.stopped.connect(self._on_sched_stopped)

        self.view.btn_sched_start.setEnabled(False)
        self.view.btn_sched_stop.setEnabled(True)
        self.view.sched_status_label.setText("Status: Starting...")
        self.view.sched_status_label.setStyleSheet("color: blue; font-weight: bold;")

        self.view.tabs.setTabEnabled(0, False)

        if self._sched_plot_window is not None:
            self._sched_plot_window.close()
        self._sched_plot_window = SchedulePlotWindow(self._sched_log_start, unit)
        self.backend.pressure_updated.connect(self._sched_plot_window.add_point)
        self._sched_plot_window.show()

        self._runner.start()

    def sched_stop(self):
        if self._runner:
            self._runner.stop()

    def _on_sched_status(self, msg: str):
        self.view.sched_status_label.setText(msg)
        self.view.sched_status_label.setStyleSheet("color: blue; font-weight: bold;")

    def _on_sched_warning(self, msg: str):
        self.view.sched_warning_label.setText(msg)
        self.view.sched_warning_label.setVisible(True)

    def _on_sched_completed(self):
        self._finish_schedule(completed=True)

    def _on_sched_stopped(self):
        self._finish_schedule(completed=False)

    def _finish_schedule(self, completed: bool):
        self._runner = None
        self._sched_logging = False
        self._flush_sched_log()
        count = self._sched_log_record_count
        path  = self._sched_log_path
        if self._sched_log_file:
            try:
                self._sched_log_file.close()
            except Exception:
                pass
            self._sched_log_file   = None
            self._sched_log_writer = None

        self.view.tabs.setTabEnabled(0, True)

        if self._sched_plot_window is not None:
            if self.backend:
                try:
                    self.backend.pressure_updated.disconnect(self._sched_plot_window.add_point)
                except Exception:
                    pass
            suffix = "Complete" if completed else "Stopped"
            self._sched_plot_window.setWindowTitle(
                f"Scheduled Control — Live Pressure [{suffix}]"
            )

        self.view.btn_sched_start.setEnabled(True)
        self.view.btn_sched_stop.setEnabled(False)
        self.view.set_pressure_unit_enabled(True)
        style = "color: green; font-weight: bold;" if completed else "color: orange; font-weight: bold;"
        self.view.sched_status_label.setStyleSheet(style)
        self.view.sched_record_label.setText(f"Records: {count}")

        if count > 0:
            title = "Schedule Complete" if completed else "Schedule Stopped"
            QMessageBox.information(
                self.view, title,
                f"Saved {count} records.\n\n{path}",
            )

    def handle_error(self, err_msg: str):
        print(f"[PACE5000] Instrument Error: {err_msg}")


# ==============================================================
# Window wrapper
# ==============================================================

class Pace5000Window(QMainWindow):
    """Top-level window for the PACE5000 app.

    When *backend* is provided it is used as-is (managed by the launcher).
    When omitted the window handles its own connection via the UI.
    """

    def __init__(self, backend: Pace5000Backend = None):
        super().__init__()
        self.setWindowTitle("Druck PACE5000 Controller")
        view = PaceUI()
        self._controller = AppController(view, backend=backend)
        self.setCentralWidget(view)
        self.resize(920, 800)

    def closeEvent(self, event):
        self._controller.disconnect_device()
        event.accept()


# ==============================================================
# Standalone entry point
# ==============================================================

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = Pace5000Window()
    window.show()
    sys.exit(app.exec())
