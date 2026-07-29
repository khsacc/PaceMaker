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

Owner lease: the API key alone only proves a client is *allowed* to talk to
the server — it does nothing to stop two authenticated clients from issuing
conflicting commands at the same time (e.g. a scripted scan and an operator
on the manual tab both calling /pressure). /api/v1/lease/* implements a
single, server-wide advisory lease: whoever holds it is the sole client
/pressure and /control_mode will accept writes from. Nobody has to acquire
it to use the API as before (a server with no lease held behaves exactly
like it always has, so this is backward compatible) — it only starts
rejecting other clients' writes once someone has actually claimed
exclusivity. This deliberately does not cover the embedded, non-HTTP usage
of Pace5000Backend (the manual control tab, Scheduled Control, and
apps/exp_scheduler in the parent bl18c_controller repo all call the backend
object directly, in-process) — extending the lease to that path needs
separate design work.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .pace5000_backend import Pace5000Backend, PRESSURE_UNIT_TO_MPA, RATE_UNIT_TO_MPA_PER_MIN

API_PREFIX = "/api/v1"
MAX_WAIT_TIMEOUT_S = 300  # cap for /pressure/wait — avoid holding a connection open indefinitely
DEFAULT_LEASE_TTL_S = 60  # /lease/acquire default if ttl_s is omitted
MAX_LEASE_TTL_S = 300  # cap — a client that vanishes must not lock the API out indefinitely
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
        elif path == f"{API_PREFIX}/lease":
            self._handle_lease_status()
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
        elif path == f"{API_PREFIX}/lease/acquire":
            self._handle_lease_acquire(body)
        elif path == f"{API_PREFIX}/lease/renew":
            self._handle_lease_renew(body)
        elif path == f"{API_PREFIX}/lease/release":
            self._handle_lease_release(body)
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
            **backend.get_status_mpa(),
            "control_mode": _to_bool(backend.get_output_state()),
        })

    def _lease_ok(self, body: dict) -> bool:
        return self.server.check_lease(body.get("token"))

    def _lease_forbidden(self) -> None:
        owner, remaining_s = self.server.lease_info()
        self._send_json(403, {
            "error": (
                f"PACE5000 is leased by {owner!r} for {remaining_s:.0f} more s — "
                f"supply the matching 'token', or wait for it to expire/be released"
            ),
        })

    def _handle_set_pressure(self, body: dict) -> None:
        backend = self._backend()
        if not backend._is_connected:
            self._send_json(409, {"error": "PACE5000 is not connected"})
            return
        if not self._lease_ok(body):
            self._lease_forbidden()
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
        if not self._lease_ok(body):
            self._lease_forbidden()
            return
        try:
            backend.set_control_mode(body["enabled"])
        except RuntimeError as e:
            self._send_json(409, {"error": str(e)})
            return
        self._send_json(200, {"ok": True, "enabled": body["enabled"]})

    def _handle_lease_status(self) -> None:
        owner, remaining_s = self.server.lease_info()
        self._send_json(200, {
            "held": owner is not None,
            "owner": owner,
            "expires_in_s": remaining_s if owner is not None else None,
        })

    def _handle_lease_acquire(self, body: dict) -> None:
        owner = body.get("owner")
        if not isinstance(owner, str) or not owner.strip():
            self._bad_request("Body must include a non-empty string 'owner'")
            return
        ttl_s = _clamp_ttl(body.get("ttl_s", DEFAULT_LEASE_TTL_S))
        if ttl_s is None:
            self._bad_request("ttl_s must be a number")
            return
        try:
            token = self.server.acquire_lease(owner, ttl_s)
        except LeaseHeldError as e:
            self._send_json(409, {"error": str(e)})
            return
        self._send_json(200, {"ok": True, "token": token, "owner": owner, "expires_in_s": ttl_s})

    def _handle_lease_renew(self, body: dict) -> None:
        token = body.get("token")
        if not isinstance(token, str) or not token:
            self._bad_request("Body must include string 'token'")
            return
        ttl_s = _clamp_ttl(body.get("ttl_s", DEFAULT_LEASE_TTL_S))
        if ttl_s is None:
            self._bad_request("ttl_s must be a number")
            return
        if not self.server.renew_lease(token, ttl_s):
            self._send_json(409, {"error": "No matching, unexpired lease for this token"})
            return
        self._send_json(200, {"ok": True, "expires_in_s": ttl_s})

    def _handle_lease_release(self, body: dict) -> None:
        token = body.get("token")
        if not isinstance(token, str) or not token:
            self._bad_request("Body must include string 'token'")
            return
        if not self.server.release_lease(token):
            self._send_json(409, {"error": "No matching lease for this token (already released or expired)"})
            return
        self._send_json(200, {"ok": True})

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


def _to_bool(raw) -> bool | None:
    # get_output_state() reads back the raw SCPI ":OUTP:STAT?" response,
    # which is the string "0"/"1", not a JSON bool -- send a real bool over
    # the wire so a consumer's `x is False` (or similarly strict) check
    # doesn't silently treat "0" as truthy/enabled.
    if raw is None:
        return None
    try:
        return bool(int(raw))
    except (TypeError, ValueError):
        return None


def _clamp_ttl(raw) -> float | None:
    try:
        ttl_s = float(raw)
    except (TypeError, ValueError):
        return None
    return max(1.0, min(ttl_s, MAX_LEASE_TTL_S))


class LeaseHeldError(RuntimeError):
    """Raised by _Server.acquire_lease() when a different, unexpired lease
    is already held."""


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, backend: Pace5000Backend, api_key: str | None, require_auth: bool):
        super().__init__(address, handler)
        self.backend = backend
        self.api_key = api_key
        self.require_auth = require_auth
        # Single, server-wide advisory lease — see the module docstring's
        # "Owner lease" section. Guarded by lease_lock since requests are
        # served on separate threads (ThreadingHTTPServer).
        self.lease_lock = threading.Lock()
        self._lease_token: str | None = None
        self._lease_owner: str | None = None
        self._lease_expires_at: float = 0.0  # time.monotonic()

    def _lease_active_locked(self) -> bool:
        return self._lease_token is not None and time.monotonic() < self._lease_expires_at

    def lease_info(self) -> tuple[str | None, float]:
        """(owner, remaining_s) for the currently held, unexpired lease, or
        (None, 0.0) if no lease is held."""
        with self.lease_lock:
            if not self._lease_active_locked():
                return None, 0.0
            return self._lease_owner, self._lease_expires_at - time.monotonic()

    def check_lease(self, token: str | None) -> bool:
        """True if `token` matches the currently held lease, or if no lease
        is currently held at all — a server nobody has claimed exclusivity
        on behaves exactly as it did before /lease/* existed."""
        with self.lease_lock:
            if not self._lease_active_locked():
                return True
            return token is not None and token == self._lease_token

    def acquire_lease(self, owner: str, ttl_s: float) -> str:
        with self.lease_lock:
            if self._lease_active_locked():
                remaining = self._lease_expires_at - time.monotonic()
                raise LeaseHeldError(
                    f"PACE5000 is already leased by {self._lease_owner!r} "
                    f"for {remaining:.0f} more s"
                )
            token = secrets.token_urlsafe(16)
            self._lease_token = token
            self._lease_owner = owner
            self._lease_expires_at = time.monotonic() + ttl_s
            return token

    def renew_lease(self, token: str, ttl_s: float) -> bool:
        with self.lease_lock:
            if not self._lease_active_locked() or token != self._lease_token:
                return False
            self._lease_expires_at = time.monotonic() + ttl_s
            return True

    def release_lease(self, token: str) -> bool:
        with self.lease_lock:
            if self._lease_token is None or token != self._lease_token:
                return False
            self._lease_token = None
            self._lease_owner = None
            self._lease_expires_at = 0.0
            return True


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
