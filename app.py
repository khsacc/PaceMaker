"""
Standalone entry point for the PACE5000 controller GUI.

Run as:  python app.py                      (from this directory)
         python apps/PACE5000/app.py         (if embedded inside bl18c_controller)

This directory (apps/PACE5000/) is developed as its own git repository
(PaceMaker) and is *also* checked out as a submodule inside bl18c_controller
at apps/PACE5000/ — so this file must work whether or not an enclosing
`apps` package exists on disk. It cannot assume a project root two
directories up the way bl18c_controller's own main.py does.

Instead, it registers *this directory itself* as an in-memory package under
a private, fixed name — regardless of what the checkout folder is actually
called — and imports pace5000_app as a submodule of that package. That
gives pace5000_app.py's own relative imports (`from .pace5000_ui_main
import ...`) a real `__package__` to resolve against, with no dependency on
an enclosing `apps.PACE5000` package or any sys.path guessing about where
the project root is.

pace5000_app.py and its siblings never do their own sys.path/import
fallback tricks — this file is the single place that decides how to make
this directory importable, so the same files can never end up loaded
twice under two different module identities in one process.

Embedded use (bl18c_controller/main.py) is unaffected: it imports these
modules via the real `apps.PACE5000.*` package path, which is a completely
separate code path from this file (the two are never used in the same
process).
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_NAME = "_pace5000_standalone"

if _PKG_NAME not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _PKG_NAME, os.path.join(_HERE, "__init__.py"),
        submodule_search_locations=[_HERE],
    )
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules[_PKG_NAME] = _pkg
    _spec.loader.exec_module(_pkg)

from PyQt6.QtWidgets import QApplication

Pace5000Window = importlib.import_module(f"{_PKG_NAME}.pace5000_app").Pace5000Window


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
