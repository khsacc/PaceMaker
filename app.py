"""
Standalone entry point for the PACE5000 controller GUI.

Run as:  python apps/PACE5000/app.py [--api ...]

This is the only supported way to launch the app standalone. It puts the
bl18c_controller project root on sys.path and imports pace5000_app by its
fully-qualified package name (apps.PACE5000.pace5000_app), so that module —
and its own sibling imports of pace5000_ui_main / pace5000_backend /
pace5000_api — always resolve through the same `apps.PACE5000` package
identity as the embedded (main.py-launched) code path. Running
pace5000_app.py directly instead of this file is not supported: it would
need its own sys.path fallback, which risks this package being imported
twice under two different identities (see pace5000_app.py's top-of-file
comment).
"""
from __future__ import annotations

import argparse
import os
import sys

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

from PyQt6.QtWidgets import QApplication

from apps.PACE5000.pace5000_app import Pace5000Window


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PACE5000 standalone controller")
    parser.add_argument("--api", action="store_true",
                         help="Auto-start the HTTP API server once the device connection succeeds")
    parser.add_argument("--api-host", default=None,
                         help="API server bind host (default: saved setting, else 127.0.0.1)")
    parser.add_argument("--api-port", type=int, default=None,
                         help="API server bind port (default: saved setting, else 8765)")
    parser.add_argument("--api-key", default=None,
                         help="API key (required when --api-host is not loopback)")
    args, qt_args = parser.parse_known_args()

    app = QApplication([sys.argv[0]] + qt_args)
    app.setStyle("Fusion")
    api_cli = {"host": args.api_host, "port": args.api_port, "key": args.api_key} if args.api else None
    window = Pace5000Window(api_cli=api_cli)
    window.show()
    sys.exit(app.exec())
