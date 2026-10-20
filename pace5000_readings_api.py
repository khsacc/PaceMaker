"""Local, read-only HTTP access to PACE5000's last polled readings."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


DEFAULT_PORT = 8766


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if urlparse(self.path).path != "/api/v1/readings":
            self.send_error(404)
            return
        body = json.dumps(self.server.backend.get_cached_readings(), allow_nan=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, backend):
        super().__init__(address, _Handler)
        self.backend = backend


class Pace5000ReadingsServer:
    def __init__(self, backend, port: int = DEFAULT_PORT):
        self._backend = backend
        self._port = port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = _Server(("127.0.0.1", self._port), self._backend)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        try:
            self._thread.start()
        except Exception:
            self._server.server_close()
            self._server = None
            self._thread = None
            raise

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)
        self._thread = None
        self._server = None

    @property
    def listen_url(self) -> str:
        port = self._server.server_port if self._server is not None else self._port
        return f"http://127.0.0.1:{port}/api/v1/readings"
