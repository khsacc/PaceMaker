from __future__ import annotations

import os
import csv
import json
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QMessageBox, QFileDialog, QWidget, QVBoxLayout, QGroupBox,
)
from PyQt6.QtCore import QObject, QTimer, pyqtSignal, Qt
import pyqtgraph as pg

# This module is only ever imported as part of a package — never run
# directly, and never imported via a bare/sys.path-hacked fallback. Two
# call sites do this:
#   - main.py (embedded mode) imports it as `apps.PACE5000.pace5000_app`.
#   - apps/PACE5000/app.py (the standalone launcher) registers this
#     directory as an in-memory package under a private name and imports
#     it as a submodule of that — this works whether or not apps/PACE5000/
#     happens to sit inside an enclosing `apps` package, e.g. when this
#     directory's own repository (PaceMaker) is git-cloned on its own.
# Either way this module always has a real __package__, so relative imports
# resolve correctly and these files are never loaded twice under two
# different module identities in the same process. Always use plain
# relative imports here.
from .pace5000_ui_main import PaceUI
from .pace5000_backend import (
    Pace5000Backend, PRESSURE_UNIT_TO_MPA, RATE_UNIT_TO_MPA_PER_MIN,
    MIN_SLEW_RATE_MPA_PER_SEC, rate_to_mpa_per_sec,
)
from .pace5000_api import Pace5000ApiServer, generate_api_key


def _format_num(value: float) -> str:
    return f"{value:.6g}"


_SETTINGS_PATH = Path(__file__).parent / "__localdata" / "pace5000_settings.json"
_DEFAULT_SETTINGS = {
    "connection_type": 0,
    "ip_address": "192.168.1.100",
    "port": "5025",
    "com_port": "COM1",
    "baudrate": "9600",
    "last_log_dir": "",
    "api_host": "127.0.0.1",
    "api_port": "8765",
    "api_key": "",
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
# API Server — Configuration Subwindow
# ==============================================================

class ApiConfigWindow(QWidget):
    """Standalone-only subwindow hosting the API Server configuration group.

    Kept out of the main window (opened via the "API" menu) so it doesn't
    take up permanent space for the majority of users who never touch it.
    """

    def __init__(self, api_group: QGroupBox, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("PACE5000 — API Server Configuration")
        layout = QVBoxLayout(self)
        layout.addWidget(api_group)
        self.resize(560, 150)


# ==============================================================
# Scheduled Control Runner
# ==============================================================

class ScheduledControlRunner(QObject):
    status_changed   = pyqtSignal(str)
    item_activated   = pyqtSignal(int)
    pressure_warning = pyqtSignal(str)
    error_occurred   = pyqtSignal(str)
    completed        = pyqtSignal()
    stopped          = pyqtSignal()

    # Internal, cross-thread plumbing only (see _wait_pressure_worker below).
    # Not part of the public interface — external code should connect to the
    # signals above, not these.
    _pressure_progress   = pyqtSignal(float, float)   # current_mpa, target_mpa
    _pressure_reached    = pyqtSignal()
    _pressure_wait_error = pyqtSignal(str)

    _ETA_WARNING_SEC = 300
    _DEFAULT_PRESSURE_TOL_MPA = 0.0001

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

        self._target_pressure_mpa = 0.0
        self._target_display_str  = ""
        self._pressure_eta        = None
        self._eta_warning_sent    = False

        # Pressure-reached waiting runs in a background thread (it can take
        # many minutes) — see Pace5000Backend.wait_for_pressure(). The
        # _pressure_* signals marshal results back onto this QObject's own
        # thread (the GUI main thread), per the project convention that UI
        # state must only be touched from the main thread.
        self._pressure_thread: threading.Thread | None = None
        self._pressure_stop_event = threading.Event()
        self._pressure_progress.connect(self._on_pressure_progress)
        self._pressure_reached.connect(self._on_pressure_reached)
        self._pressure_wait_error.connect(self._on_pressure_wait_error)

    def start(self):
        self._running = True
        self.current_index = 0
        self._tick_timer.start()
        self._execute_current()

    def stop(self):
        self._running = False
        self._wait_timer.stop()
        self._tick_timer.stop()
        self._pressure_stop_event.set()
        self.status_changed.emit("Status: Stopped")
        self.stopped.emit()

    def _execute_current(self):
        if not self._running:
            return
        if self.current_index >= len(self.items):
            self._tick_timer.stop()
            self.status_changed.emit("Status: Complete ✓")
            self.completed.emit()
            return

        item  = self.items[self.current_index]
        n     = self.current_index + 1
        total = len(self.items)
        self.item_activated.emit(self.current_index)

        if item["type"] == "wait":
            self._wait_duration = item["duration"]
            self._wait_start    = time.time()
            self._wait_timer.start(int(self._wait_duration * 1000))
            self.status_changed.emit(
                f"Running [{n}/{total}]: Wait {self._wait_duration:.1f} s"
            )

        elif item["type"] == "change_pressure":
            self._start_change_pressure(item, n, total)

    def _start_change_pressure(self, item: dict, n: int, total: int):
        # set_pressure_with_ramp() / wait_for_pressure() are the single
        # shared implementation (apps/PACE5000/pace5000_backend.py), also
        # used by apps/exp_scheduler and the HTTP API. In particular,
        # set_pressure_with_ramp() sends the slew rate and verifies the
        # device applied it *before* sending the setpoint — sending them in
        # the other order (or without verifying) risks the new setpoint
        # being approached at whatever rate was previously in effect.
        pressure_mpa = item["pressure"] * PRESSURE_UNIT_TO_MPA.get(item["pressure_unit"], 1.0)
        rate_mpa_per_min = item["rate"] * RATE_UNIT_TO_MPA_PER_MIN.get(item["rate_unit"], 1.0)

        before = self.backend.get_pressure()
        if before is not None and rate_mpa_per_min > 0:
            delta = abs(pressure_mpa - before)
            self._pressure_eta = time.time() + delta / (rate_mpa_per_min / 60.0)
        else:
            self._pressure_eta = None
        self._eta_warning_sent = False

        self._target_pressure_mpa = pressure_mpa
        self._target_display_str  = f"{item['pressure']:.4g} {item['pressure_unit']}"

        try:
            self.backend.set_pressure_with_ramp(pressure_mpa, rate_mpa_per_min)
        except RuntimeError as e:
            self.error_occurred.emit(f"Scheduled Control: {e}")
            self.stop()
            return

        self.status_changed.emit(
            f"Running [{n}/{total}]: Pressure → {self._target_display_str} (monitoring...)"
        )

        self._pressure_stop_event.clear()
        self._pressure_thread = threading.Thread(
            target=self._wait_pressure_worker, daemon=True,
        )
        self._pressure_thread.start()

    def _wait_pressure_worker(self):
        """Runs on a background thread — must not touch UI/self.view state."""
        try:
            result = self.backend.wait_for_pressure(
                self._DEFAULT_PRESSURE_TOL_MPA,
                stop_event=self._pressure_stop_event,
                on_update=lambda cur, tgt: self._pressure_progress.emit(cur, tgt),
            )
        except Exception as e:
            self._pressure_wait_error.emit(str(e))
            return
        if result is None:
            return  # cancelled via stop() — stop() already emitted `stopped`
        self._pressure_reached.emit()

    def _on_pressure_progress(self, current_mpa: float, target_mpa: float):
        if not self._running:
            return
        n, total = self.current_index + 1, len(self.items)
        current_disp = f"{current_mpa:.4g} MPa"
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

    def _on_pressure_reached(self):
        if not self._running:
            return
        self.current_index += 1
        self._execute_current()

    def _on_pressure_wait_error(self, msg: str):
        if not self._running:
            return
        self.error_occurred.emit(f"Scheduled Control: {msg}")
        self.stop()

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


# ==============================================================
# App Controller
# ==============================================================

class AppController:
    # Sustained-saturation warning thresholds for the Effort readout — same
    # "edge-derived, recompute every tick" pattern as
    # ScheduledControlRunner._ETA_WARNING_SEC, just for a continuously
    # updated label rather than a one-shot popup.
    _EFFORT_WARNING_THRESHOLD_PERCENT = 90.0
    _EFFORT_WARNING_SUSTAINED_S = 30.0

    def __init__(self, view: PaceUI, backend: Pace5000Backend = None, api_cli: dict | None = None):
        self.view    = view
        self._runner = None
        self._owns_backend = backend is None

        # HTTP API server — standalone-only (see api_group visibility below).
        # api_cli, when given, comes from CLI flags (--api/--api-host/...) and
        # auto-starts the server once the device connection succeeds.
        self.api_server: Pace5000ApiServer | None = None
        self._api_cli = api_cli if self._owns_backend else None

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
        self._rate_time_unit = "sec"
        self._sched_plot_window: SchedulePlotWindow | None = None
        self._last_target_str = ""
        self._positive_source: float | None = None
        self._negative_source: float | None = None
        self._live_target_native: float | None = None
        self._live_slew_native_per_sec: float | None = None
        self._effort_saturated_since: float | None = None
        self._instrument_full_scale_mpa: float | None = None

        if backend is not None:
            # Connection managed by the launcher — wire signals and hide connection UI
            self.backend = backend
            backend.connection_status_changed.connect(self.handle_connection_status)
            backend.pressure_updated.connect(self.update_plot)
            backend.source_pressures_updated.connect(self.update_source_pressures)
            backend.setpoint_updated.connect(self._on_setpoint_updated)
            backend.effort_updated.connect(self._on_effort_updated)
            backend.error_occurred.connect(self.handle_error)
            backend.instrument_error_reported.connect(self.handle_instrument_error)
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
        self.view.target_input.textEdited.connect(self._on_target_input_edited)
        self.view.radio_unit_mpa.toggled.connect(
            lambda checked: checked and self._on_target_pressure_unit_changed("MPa")
        )
        self.view.radio_unit_bar.toggled.connect(
            lambda checked: checked and self._on_target_pressure_unit_changed("Bar")
        )
        self.view.rate_input.returnPressed.connect(self.update_rate)
        self.view.rate_input.textEdited.connect(self._on_rate_input_edited)
        self.view.rate_time_combo.currentTextChanged.connect(self._on_rate_time_unit_changed)
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

        self.view.api_enable_cb.toggled.connect(self._on_api_toggled)
        self.view.btn_api_regenerate.clicked.connect(self._on_api_regenerate)
        self.view.btn_api_copy.clicked.connect(self._on_api_copy)

    def _apply_settings(self, s: dict):
        self.view.conn_type_combo.setCurrentIndex(s["connection_type"])
        self.view.ip_input.setText(s["ip_address"])
        self.view.port_input.setText(s["port"])
        self.view.com_port_input.setText(s["com_port"])
        self.view.baudrate_input.setText(s["baudrate"])
        self.view.api_host_input.setText(s.get("api_host", "127.0.0.1"))
        try:
            self.view.api_port_spin.setValue(int(s.get("api_port", 8765)))
        except (TypeError, ValueError):
            pass
        self.view.api_key_input.setText(s.get("api_key", ""))

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
        self.backend.setpoint_updated.connect(self._on_setpoint_updated)
        self.backend.effort_updated.connect(self._on_effort_updated)
        self.backend.error_occurred.connect(self.handle_error)
        self.backend.instrument_error_reported.connect(self.handle_instrument_error)
        self.view.status_label.setText("Status: Connecting...")
        self.backend.connect_device()

    def disconnect_device(self):
        self._stop_api_server()
        if self._logging_active:
            self.stop_logging()
        if self._runner:
            self._runner.stop()
            self._runner = None
        if self._owns_backend and self.backend:
            self.backend.stop()
            self.backend = None

    # ---------------------------------------------------------- API server
    # Standalone-only (api_group is hidden entirely in embedded mode, and
    # api_enable_cb only becomes enabled once connected — see
    # handle_connection_status below).

    def _start_api_server(self, host: str, port: int, key: str | None) -> bool:
        try:
            self.api_server = Pace5000ApiServer(self.backend, host=host, port=port, api_key=key or None)
            self.api_server.start()
        except (ValueError, OSError) as e:
            QMessageBox.critical(self.view, "API Server Error", str(e))
            self.api_server = None
            return False
        _save_settings({**_load_settings(), "api_host": host, "api_port": str(port), "api_key": key or ""})
        self.view.api_host_input.setText(host)
        self.view.api_port_spin.setValue(port)
        self.view.api_key_input.setText(key or "")
        self.view.api_status_label.setText(f"Running: {self.api_server.listen_url}")
        self.view.api_status_label.setStyleSheet("color: green; font-weight: bold;")
        self.view.api_host_input.setEnabled(False)
        self.view.api_port_spin.setEnabled(False)
        self.view.btn_api_regenerate.setEnabled(False)
        self._set_control_tabs_enabled(False)
        return True

    def _stop_api_server(self):
        if self.api_server is not None:
            self.api_server.stop()
            self.api_server = None
        self.view.api_status_label.setText("Stopped")
        self.view.api_status_label.setStyleSheet("color: gray; font-weight: bold;")
        self.view.api_host_input.setEnabled(True)
        self.view.api_port_spin.setEnabled(True)
        self.view.btn_api_regenerate.setEnabled(True)
        self.view.api_enable_cb.blockSignals(True)
        self.view.api_enable_cb.setChecked(False)
        self.view.api_enable_cb.blockSignals(False)
        self._set_control_tabs_enabled(True)

    def _set_control_tabs_enabled(self, enabled: bool):
        # While the HTTP API is enabled, the device can be driven from another
        # process at any time — Manual Control and Scheduled Control must not
        # race against that, so both tabs are locked out entirely.
        self.view.tabs.setTabEnabled(0, enabled)
        self.view.tabs.setTabEnabled(1, enabled)

    def _on_api_toggled(self, checked: bool):
        if checked:
            host = self.view.api_host_input.text().strip() or "127.0.0.1"
            port = self.view.api_port_spin.value()
            key  = self.view.api_key_input.text().strip()
            if not self._start_api_server(host, port, key or None):
                self.view.api_enable_cb.blockSignals(True)
                self.view.api_enable_cb.setChecked(False)
                self.view.api_enable_cb.blockSignals(False)
        else:
            self._stop_api_server()

    def _on_api_regenerate(self):
        key = generate_api_key()
        self.view.api_key_input.setText(key)
        _save_settings({**_load_settings(), "api_key": key})

    def _on_api_copy(self):
        QApplication.clipboard().setText(self.view.api_key_input.text())

    # ----------------------------------------------------------
    def handle_connection_status(self, connected: bool):
        if connected:
            self.view.status_label.setText("Status: Connected")
            self.view.status_label.setStyleSheet("color: green; font-weight: bold;")
            self.view.instrument_error_label.setText("Last Instrument Error: none")
            self.view.instrument_error_label.setStyleSheet("color: gray; font-weight: bold;")
            self.view.btn_connect.setEnabled(False)
            self.view.btn_disconnect.setEnabled(True)
            self.view.btn_log_start.setEnabled(True)
            self.view.api_enable_cb.setEnabled(True)
            interval_ms = int(self.view.interval_spinbox.value() * 1000)
            self.backend.timer.setInterval(interval_ms)
            QTimer.singleShot(100, self._do_initial_fetch)
            if self._api_cli is not None and self.api_server is None:
                host = self._api_cli.get("host") or self.view.api_host_input.text().strip() or "127.0.0.1"
                port = self._api_cli.get("port") or self.view.api_port_spin.value()
                key = self._api_cli.get("key") or self.view.api_key_input.text().strip() or None
                if self._start_api_server(host, port, key):
                    self.view.api_enable_cb.blockSignals(True)
                    self.view.api_enable_cb.setChecked(True)
                    self.view.api_enable_cb.blockSignals(False)
        else:
            self.view.status_label.setText("Status: Disconnected")
            self.view.status_label.setStyleSheet("color: red; font-weight: bold;")
            self.view.source_pressure_label.setText("−ve source:  ---    +ve source:  ---")
            self.view.setpoint_live_label.setText("Target Pressure:  ---    Slew Rate:  ---")
            self.view.effort_label.setText("Effort:  ---  %")
            self.view.effort_status_label.setText("Effort: ---")
            self.view.effort_status_label.setStyleSheet("color: gray; font-weight: bold;")
            self.view.instrument_full_scale_label.setText("Instrument full-scale: ---")
            self._positive_source = None
            self._negative_source = None
            self._live_target_native = None
            self._live_slew_native_per_sec = None
            self._effort_saturated_since = None
            self._instrument_full_scale_mpa = None
            self.view.btn_connect.setEnabled(True)
            self.view.btn_disconnect.setEnabled(False)
            self.view.btn_log_start.setEnabled(False)
            self.view.btn_log_stop.setEnabled(False)
            self.view.api_enable_cb.setEnabled(False)
            self._stop_api_server()
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

    def _on_effort_updated(self, effort: float):
        now = time.time()
        saturated = abs(effort) >= self._EFFORT_WARNING_THRESHOLD_PERCENT
        if not saturated:
            self._effort_saturated_since = None
        elif self._effort_saturated_since is None:
            self._effort_saturated_since = now
        sustained = (
            saturated
            and self._effort_saturated_since is not None
            and now - self._effort_saturated_since >= self._EFFORT_WARNING_SUSTAINED_S
        )

        self.view.effort_label.setText(f"Effort:  {effort:.1f}  %")

        if sustained:
            cause = (
                "supply valve maxed — check source pressure / slew rate"
                if effort > 0 else
                "vacuum valve maxed — check for leaks / blocked line"
            )
            elapsed = now - self._effort_saturated_since
            self.view.effort_status_label.setText(
                f"⚠ Effort saturated {elapsed:.0f}s: {effort:.1f}% ({cause})"
            )
            self.view.effort_status_label.setStyleSheet("color: #b00; font-weight: bold;")
        elif saturated:
            self.view.effort_status_label.setText(f"Effort: {effort:.1f}%")
            self.view.effort_status_label.setStyleSheet("color: #b06a00; font-weight: bold;")
        else:
            self.view.effort_status_label.setText(f"Effort: {effort:.1f}%")
            self.view.effort_status_label.setStyleSheet("color: gray; font-weight: bold;")

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

    def _read_and_validate_rate(self) -> tuple[float, str, float] | None:
        """Parse + validate the rate input field against the hardware floor.

        Returns (rate_val, rate_unit, rate_per_min) in the operator's
        currently selected pressure/time units, or None (after showing a
        QMessageBox) if the field is invalid or below MIN_SLEW_RATE_MPA_PER_SEC.
        """
        try:
            rate_val = float(self.view.rate_input.text())
        except ValueError:
            QMessageBox.warning(self.view, "Error", "Invalid rate value. Numbers only.")
            return None
        pressure_unit = self.view.get_pressure_unit()
        time_unit = self.view.rate_time_combo.currentText()
        rate_unit = f"{pressure_unit}/{time_unit}"
        if rate_to_mpa_per_sec(rate_val, rate_unit) < MIN_SLEW_RATE_MPA_PER_SEC:
            QMessageBox.warning(
                self.view, "Error",
                f"Slew rate is below the minimum allowed "
                    f"({MIN_SLEW_RATE_MPA_PER_SEC:.3f} MPa/sec).\n"
                    f"Entered value: {rate_val:.4g} {rate_unit}."
            )
            return None
        rate_per_min = rate_val if time_unit == "min" else rate_val * 60.0
        return rate_val, rate_unit, rate_per_min

    def _read_max_safe_pressure_mpa(self) -> float | None:
        """Parse the optional Max Safe Pressure field into MPa.

        Returns None if left blank (no ceiling configured) — an unparseable
        value is also treated as "no ceiling" since this is a soft,
        user-editable safety aid, not a required field, and should never
        block a pressure change with a confusing error of its own.
        """
        text = self.view.max_safe_pressure_input.text().strip()
        if not text:
            return None
        try:
            val = float(text)
        except ValueError:
            return None
        return val * PRESSURE_UNIT_TO_MPA[self.view.get_pressure_unit()]

    def update_target(self):
        if not self.backend or not self.backend._is_connected:
            QMessageBox.warning(self.view, "Error", "Device not connected!")
            return
        try:
            val = float(self.view.target_input.text())
        except ValueError:
            QMessageBox.warning(self.view, "Error", "Invalid target pressure. Numbers only.")
            return
        unit = self.view.get_pressure_unit()
        rate_result = self._read_and_validate_rate()
        if rate_result is None:
            return
        rate_val, rate_unit, rate_per_min = rate_result
        if self._positive_source is not None and val > self._positive_source:
            QMessageBox.warning(
                self.view, "Target Exceeds +ve Source",
                f"Set value ({val:.4g} {unit}) exceeds +ve source pressure "
                    f"({self._positive_source:.4g} {unit}).\n"
                    f"Target has not been updated."
            )
            self.view.target_input.setText(self._last_target_str)
            self.view.target_input.setStyleSheet("")
            return
        max_safe_mpa = self._read_max_safe_pressure_mpa()
        if max_safe_mpa is not None and val * PRESSURE_UNIT_TO_MPA[unit] > max_safe_mpa:
            QMessageBox.warning(
                self.view, "Target Exceeds Max Safe Pressure",
                f"Set value ({val:.4g} {unit}) exceeds the configured Max Safe Pressure "
                    f"({max_safe_mpa / PRESSURE_UNIT_TO_MPA[unit]:.4g} {unit}).\n"
                    f"Target has not been updated."
            )
            self.view.target_input.setText(self._last_target_str)
            self.view.target_input.setStyleSheet("")
            return
        if self.view.chk_confirm_before_apply.isChecked():
            msg = f"Go to {val} {unit} at {rate_val:.4g} {rate_unit}?"
            reply = QMessageBox.question(
                self.view, "Confirm", msg,
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Ok:
                return
        # set_pressure_with_ramp() sends the slew rate and verifies the
        # device applied it (read-back) *before* sending the setpoint —
        # the same ordering guarantee Scheduled Control and exp_scheduler
        # rely on, now unified into the manual "Apply" path too.
        try:
            self.backend.set_pressure_with_ramp(val, rate_per_min, unit=unit)
        except RuntimeError as e:
            QMessageBox.warning(self.view, "Error", str(e))
            self.view.target_input.setText(self._last_target_str)
            self.view.target_input.setStyleSheet("")
            return
        self._last_target_str = self.view.target_input.text()
        self.view.target_input.setStyleSheet("")
        self.view.rate_input.setStyleSheet("")
        QMessageBox.information(
            self.view, "Success",
            f"Target updated to: {val} {unit} at {rate_val:.4g} {rate_unit} (rate verified).",
        )

    def update_rate(self):
        if not self.backend or not self.backend._is_connected:
            QMessageBox.warning(self.view, "Error", "Device not connected!")
            return
        rate_result = self._read_and_validate_rate()
        if rate_result is None:
            return
        rate_val, rate_unit, _ = rate_result
        self.backend.set_slew_rate(rate_val, rate_unit)
        self.view.rate_input.setStyleSheet("")
        QMessageBox.information(self.view, "Success", f"Rate updated to: {rate_val} {rate_unit}")

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
        rate_result = self._read_and_validate_rate()
        if rate_result is None:
            return
        rate_val, rate_unit, rate_per_min = rate_result

        new_val = current + sign * step
        unit    = self.view.get_pressure_unit()
        if self._positive_source is not None and new_val > self._positive_source:
            QMessageBox.warning(
                self.view, "Target Exceeds +ve Source",
                f"Set value ({new_val:.4g} {unit}) exceeds +ve source pressure "
                    f"({self._positive_source:.4g} {unit}).\n"
                    f"Target has not been updated."
            )
            return
        max_safe_mpa = self._read_max_safe_pressure_mpa()
        if max_safe_mpa is not None and new_val * PRESSURE_UNIT_TO_MPA[unit] > max_safe_mpa:
            QMessageBox.warning(
                self.view, "Target Exceeds Max Safe Pressure",
                f"Set value ({new_val:.4g} {unit}) exceeds the configured Max Safe Pressure "
                    f"({max_safe_mpa / PRESSURE_UNIT_TO_MPA[unit]:.4g} {unit}).\n"
                    f"Target has not been updated."
            )
            return
        try:
            self.backend.set_pressure_with_ramp(new_val, rate_per_min, unit=unit)
        except RuntimeError as e:
            QMessageBox.warning(self.view, "Error", str(e))
            return
        self._last_target_str = f"{new_val:.4g}"
        self.view.target_input.setText(f"{new_val:.4g}")
        self.view.target_input.setStyleSheet("")
        self.view.rate_input.setStyleSheet("")

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
            self.view.target_input.setStyleSheet("")
            self._last_target_str = setpoint
        slew_raw = self.backend.get_slew_rate()
        if setpoint is not None and slew_raw is not None:
            try:
                self._on_setpoint_updated(float(setpoint), float(slew_raw))
            except ValueError:
                pass
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
        full_scale_mpa = self.backend.get_control_full_scale_mpa()
        if full_scale_mpa is not None:
            self._instrument_full_scale_mpa = full_scale_mpa
            unit = self.view.get_pressure_unit()
            display_val = full_scale_mpa / PRESSURE_UNIT_TO_MPA[unit]
            self.view.instrument_full_scale_label.setText(
                f"Instrument full-scale: {display_val:.4g} {unit}"
            )
            if not self.view.max_safe_pressure_input.text().strip():
                self.view.max_safe_pressure_input.setText(_format_num(display_val))

    def _on_target_input_edited(self, _text: str):
        self.view.target_input.setStyleSheet("background-color: #e6ffe6;")

    def _on_rate_input_edited(self, _text: str):
        self.view.rate_input.setStyleSheet("background-color: #e6ffe6;")

    def _on_setpoint_updated(self, target_native: float, slew_native_per_sec: float):
        self._live_target_native = target_native
        self._live_slew_native_per_sec = slew_native_per_sec
        self._render_live_setpoints()

    def _render_live_setpoints(self):
        if self._live_target_native is None or self._live_slew_native_per_sec is None:
            return
        unit = self.view.get_pressure_unit()
        time_unit = self.view.rate_time_combo.currentText()
        slew_val = self._live_slew_native_per_sec * (60.0 if time_unit == "min" else 1.0)
        self.view.setpoint_live_label.setText(
            f"Target Pressure:  {self._live_target_native:.4g} {unit}    "
                f"Slew Rate:  {slew_val:.4g} {unit}/{time_unit}"
        )

    def _convert_line_edit_value(self, line_edit, factor: float):
        text = line_edit.text().strip()
        if not text:
            return
        try:
            val = float(text)
        except ValueError:
            return
        line_edit.setText(_format_num(val * factor))

    def _on_target_pressure_unit_changed(self, unit: str):
        self.view.rate_pressure_unit_display.setText(unit)
        self.view.plot_widget.setLabel("left", f"Pressure ({unit})")
        if self.backend and self.backend._is_connected:
            scpi_unit = "MPA" if unit == "MPa" else "BAR"
            self.backend.write(f":UNIT:PRES {scpi_unit}")
        old_unit = self._logging_unit
        if old_unit != unit:
            factor = PRESSURE_UNIT_TO_MPA[old_unit] / PRESSURE_UNIT_TO_MPA[unit]
            self.pressure_data = deque(p * factor for p in self.pressure_data)
            if self.pressure_data:
                self.view.plot_curve.setData(
                    [(v - self.time_data[0]) for v in self.time_data],
                    list(self.pressure_data),
                )
            self._convert_line_edit_value(self.view.target_input, factor)
            self._convert_line_edit_value(self.view.rate_input, factor)
            self._convert_line_edit_value(self.view.max_safe_pressure_input, factor)
            if self._live_target_native is not None:
                self._live_target_native *= factor
            if self._live_slew_native_per_sec is not None:
                self._live_slew_native_per_sec *= factor
            self._logging_unit = unit
        if self._instrument_full_scale_mpa is not None:
            display_val = self._instrument_full_scale_mpa / PRESSURE_UNIT_TO_MPA[unit]
            self.view.instrument_full_scale_label.setText(
                f"Instrument full-scale: {display_val:.4g} {unit}"
            )
        self._render_live_setpoints()

    def _on_rate_time_unit_changed(self, new_time_unit: str):
        old_time_unit = self._rate_time_unit
        if old_time_unit != new_time_unit:
            factor = 60.0 if (old_time_unit == "sec" and new_time_unit == "min") else (1.0 / 60.0)
            self._convert_line_edit_value(self.view.rate_input, factor)
            self._rate_time_unit = new_time_unit
        self._render_live_setpoints()

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
            rate_unit = self.view.sched_rate_unit.currentText()
            if rate_to_mpa_per_sec(rate, rate_unit) < MIN_SLEW_RATE_MPA_PER_SEC:
                QMessageBox.warning(
                    self.view, "Error",
                    f"Slew rate is below the minimum allowed "
                        f"({MIN_SLEW_RATE_MPA_PER_SEC:.3f} MPa/sec).\n"
                        f"Entered value: {rate:.4g} {rate_unit}."
                )
                return
            pressure_unit = self.view.sched_pressure_unit.currentText()
            max_safe_mpa = self._read_max_safe_pressure_mpa()
            if max_safe_mpa is not None and pressure * PRESSURE_UNIT_TO_MPA.get(pressure_unit, 1.0) > max_safe_mpa:
                max_safe_display = max_safe_mpa / PRESSURE_UNIT_TO_MPA.get(pressure_unit, 1.0)
                QMessageBox.warning(
                    self.view, "Error",
                    f"Pressure ({pressure:.4g} {pressure_unit}) exceeds the configured "
                        f"Max Safe Pressure ({max_safe_display:.4g} {pressure_unit})."
                )
                return
            new_item = {
                "type":          "change_pressure",
                "pressure":      pressure,
                "pressure_unit": pressure_unit,
                "rate":          rate,
                "rate_unit":     rate_unit,
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
        self._runner.error_occurred.connect(self._on_sched_error)
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

    def _on_sched_error(self, msg: str):
        QMessageBox.critical(self.view, "Scheduled Control Error", msg)

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

    def handle_instrument_error(self, code: int, message: str):
        print(f"[PACE5000] SYST:ERR {code}: {message}")
        self.view.instrument_error_label.setText(f"Last Instrument Error: {code} {message}")
        self.view.instrument_error_label.setStyleSheet("color: red; font-weight: bold;")


# ==============================================================
# Window wrapper
# ==============================================================

class Pace5000Window(QMainWindow):
    """Top-level window for the PACE5000 app.

    When *backend* is provided it is used as-is (managed by the launcher).
    When omitted the window handles its own connection via the UI.
    """

    def __init__(self, backend: Pace5000Backend = None, api_cli: dict | None = None):
        super().__init__()
        self.setWindowTitle("Druck PACE5000 Controller")
        view = PaceUI()
        self._controller = AppController(view, backend=backend, api_cli=api_cli)
        self.setCentralWidget(view)
        self.resize(920, 800)
        self._api_config_window: ApiConfigWindow | None = None
        self._setup_api_menu()

    def _setup_api_menu(self):
        api_menu = self.menuBar().addMenu("API")
        configure_action = api_menu.addAction("Configure and start API")
        configure_action.triggered.connect(self._open_api_config_window)
        if not self._controller._owns_backend:
            # API server is standalone-only — hide the menu entirely when the
            # connection is managed externally (embedded mode, e.g. main.py).
            api_menu.menuAction().setVisible(False)

    def _open_api_config_window(self):
        if self._api_config_window is None:
            self._api_config_window = ApiConfigWindow(self._controller.view.api_group, self)
        self._api_config_window.show()
        self._api_config_window.raise_()
        self._api_config_window.activateWindow()

    def closeEvent(self, event):
        self._controller.disconnect_device()
        event.accept()


# The standalone launcher (argparse + QApplication + event loop) lives in
# apps/PACE5000/app.py — run that file directly, not this one. See the
# module docstring-equivalent comment at the top of this file for why.
