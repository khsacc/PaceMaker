"""
PACE5000 HTTP API — optional network control/monitoring layer.

Only meant to be enabled when the app is run standalone via
apps/PACE5000/app.py (see Pace5000ApiServer usage in pace5000_app.py's
AppController). Not wired into the embedded (main.py-launched) code path.

Implemented with the standard library only (http.server) — no new
dependency beyond what this app already requires (PyQt6, pyqtgraph,
pyserial). All business logic (safety guard, slew-rate verification,
pressure-reached polling) lives in Pace5000Backend.set_pressure_with_ramp()
/ wait_for_pressure() — this module is a thin JSON/HTTP wrapper around it and
must not re-implement that logic.

Auth model: binding to loopback (127.0.0.1 / localhost / ::1) requires no
API key. Binding to any other host (i.e. reachable from the LAN) requires a
non-empty api_key, checked against the `X-API-Key` request header on every
endpoint except /health.
"""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .pace5000_backend import Pace5000Backend, PRESSURE_UNIT_TO_MPA, RATE_UNIT_TO_MPA_PER_MIN

API_PREFIX = "/api/v1"
MAX_WAIT_TIMEOUT_S = 300  # cap for /pressure/wait — avoid holding a connection open indefinitely
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def generate_api_key() -> str:
    return secrets.token_urlsafe(24)


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"

    # Silence the default stderr access log; the app already prints
    # [PACE5000] status lines and per-request noise isn't useful here.
    def log_message(self, format, *args):  # noqa: A002 (stdlib signature)
        pass

    # ── helpers ──────────────────────────────────────────────────────

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _check_auth(self) -> bool:
        if not self.server.require_auth:
            return True
        supplied = self.headers.get("X-API-Key", "")
        return secrets.compare_digest(supplied, self.server.api_key or "")

    def _unauthorized(self) -> None:
        self._send_json(401, {"error": "Missing or invalid X-API-Key header"})

    def _bad_request(self, msg: str) -> None:
        self._send_json(400, {"error": msg})

    def _not_found(self) -> None:
        self._send_json(404, {"error": "Not found"})

    def _backend(self) -> Pace5000Backend:
        return self.server.backend

    # ── routing ──────────────────────────────────────────────────────

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == f"{API_PREFIX}/health":
            self._send_json(200, {"ok": True})
            return

        if not self._check_auth():
            self._unauthorized()
            return

        if path == f"{API_PREFIX}/status":
            self._handle_status()
        elif path == f"{API_PREFIX}/pressure/wait":
            self._handle_wait_pressure(parse_qs(parsed.query))
        else:
            self._not_found()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if not self._check_auth():
            self._unauthorized()
            return

        body = self._read_json_body()
        if body is None:
            self._bad_request("Request body must be valid JSON")
            return

        if path == f"{API_PREFIX}/pressure":
            self._handle_set_pressure(body)
        elif path == f"{API_PREFIX}/control_mode":
            self._handle_set_control_mode(body)
        else:
            self._not_found()

    # ── endpoint implementations ─────────────────────────────────────

    def _handle_status(self) -> None:
        backend = self._backend()
        if not backend._is_connected:
            self._send_json(200, {"connected": False})
            return
        self._send_json(200, {
            "connected": True,
            "pressure_mpa": backend.get_pressure(),
            "target_pressure_mpa": _to_float(backend.get_target_pressure()),
            "slew_rate_mpa_per_sec": _to_float(backend.get_slew_rate()),
            "control_mode": backend.get_output_state(),
            "source_pressure_positive_mpa": backend.get_positive_source_pressure(),
            "source_pressure_negative_mpa": backend.get_negative_source_pressure(),
            "effort_percent": backend.get_effort(),
        })

    def _handle_set_pressure(self, body: dict) -> None:
        backend = self._backend()
        if not backend._is_connected:
            self._send_json(409, {"error": "PACE5000 is not connected"})
            return

        try:
            pressure = float(body["pressure"])
            rate = float(body["rate"])
        except (KeyError, TypeError, ValueError):
            self._bad_request("Body must include numeric 'pressure' and 'rate'")
            return

        unit = body.get("unit", "MPa")
        rate_unit = body.get("rate_unit", "MPa/min")
        if unit not in PRESSURE_UNIT_TO_MPA:
            self._bad_request(f"unit must be one of {sorted(PRESSURE_UNIT_TO_MPA)}")
            return
        if rate_unit not in RATE_UNIT_TO_MPA_PER_MIN:
            self._bad_request(f"rate_unit must be one of {sorted(RATE_UNIT_TO_MPA_PER_MIN)}")
            return

        pressure_mpa = pressure * PRESSURE_UNIT_TO_MPA[unit]
        rate_mpa_per_min = rate * RATE_UNIT_TO_MPA_PER_MIN[rate_unit]

        try:
            backend.set_pressure_with_ramp(pressure_mpa, rate_mpa_per_min, unit="MPa")
        except RuntimeError as e:
            self._send_json(409, {"error": str(e)})
            return

        self._send_json(200, {
            "ok": True,
            "target_pressure_mpa": pressure_mpa,
            "slew_rate_mpa_per_min": rate_mpa_per_min,
        })

    def _handle_set_control_mode(self, body: dict) -> None:
        backend = self._backend()
        if not backend._is_connected:
            self._send_json(409, {"error": "PACE5000 is not connected"})
            return
        if "enabled" not in body or not isinstance(body["enabled"], bool):
            self._bad_request("Body must include boolean 'enabled'")
            return
        backend.set_control_mode(body["enabled"])
        self._send_json(200, {"ok": True, "enabled": body["enabled"]})

    def _handle_wait_pressure(self, query: dict) -> None:
        backend = self._backend()
        if not backend._is_connected:
            self._send_json(409, {"error": "PACE5000 is not connected"})
            return

        try:
            tol = float(query.get("tol", ["0.01"])[0])
        except (ValueError, IndexError):
            self._bad_request("tol must be a number")
            return
        unit = query.get("unit", ["MPa"])[0]
        if unit not in PRESSURE_UNIT_TO_MPA:
            self._bad_request(f"unit must be one of {sorted(PRESSURE_UNIT_TO_MPA)}")
            return
        try:
            timeout_s = float(query.get("timeout_s", [str(MAX_WAIT_TIMEOUT_S)])[0])
        except (ValueError, IndexError):
            self._bad_request("timeout_s must be a number")
            return
        timeout_s = min(timeout_s, MAX_WAIT_TIMEOUT_S)
        if timeout_s < Pace5000Backend.DEFAULT_STABILITY_DWELL_S:
            self._bad_request(
                f"timeout_s must be at least "
                f"{Pace5000Backend.DEFAULT_STABILITY_DWELL_S} s — the pressure "
                f"must stay within tol continuously for that long before "
                f"being considered reached, so a shorter timeout could never "
                f"succeed"
            )
            return

        tol_mpa = tol * PRESSURE_UNIT_TO_MPA[unit]
        try:
            result = backend.wait_for_pressure(tol_mpa, timeout_s=timeout_s)
        except TimeoutError as e:
            self._send_json(408, {"error": str(e)})
            return
        except RuntimeError as e:
            self._send_json(409, {"error": str(e)})
            return

        self._send_json(200, {"ok": True, "pressure_mpa": result})


def _to_float(raw) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, backend: Pace5000Backend, api_key: str | None, require_auth: bool):
        super().__init__(address, handler)
        self.backend = backend
        self.api_key = api_key
        self.require_auth = require_auth


class Pace5000ApiServer:
    """Wraps a ThreadingHTTPServer exposing `backend` over HTTP/JSON.

    Safe to construct with a `backend` that isn't connected yet — endpoints
    that need a live connection report 409 until backend._is_connected.
    """

    def __init__(
        self,
        backend: Pace5000Backend,
        host: str = "127.0.0.1",
        port: int = 8765,
        api_key: str | None = None,
    ):
        require_auth = host not in _LOOPBACK_HOSTS
        if require_auth and not api_key:
            raise ValueError(
                "api_key is required when binding to a non-loopback host "
                f"({host!r}) — LAN-reachable servers must be authenticated."
            )
        self._backend = backend
        self._host = host
        self._port = port
        self._api_key = api_key
        self._require_auth = require_auth
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = _Server(
            (self._host, self._port), _Handler,
            backend=self._backend, api_key=self._api_key, require_auth=self._require_auth,
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        print(f"[PACE5000] API server listening on {self.listen_url}")

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._server = None
        self._thread = None
        print("[PACE5000] API server stopped")

    @property
    def is_running(self) -> bool:
        return self._server is not None

    @property
    def listen_url(self) -> str:
        return f"http://{self._host}:{self._port}"
