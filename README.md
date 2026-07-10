# PaceMaker

A PyQt6 desktop GUI for controlling and logging a **Druck PACE5000** pressure controller.

## Features

- Connect via **TCP/IP** (SCPI over port 5025) or **Serial (RS-232)**
- Set target pressure and slew rate; switch between Control / Measure modes
- Live pressure chart with configurable time window
- CSV data logging
- **Scheduled control**: build a sequence of pressure steps and waits, save/load as JSON, and run with live plot and automatic logging
- **HTTP API** (standalone mode only): control and monitor the device from another process on the same machine, or another machine on the LAN — see [API](#api) below

## Requirements

- Python 3.11+
- PyQt6
- pyqtgraph
- pyserial

Install dependencies:

```
pip install PyQt6 pyqtgraph pyserial
```

## Usage

Run from the `bl18c_controller` project root:

```
python apps/PACE5000/app.py
```

`app.py` is the only supported standalone entry point — it puts the project root on `sys.path` and imports the rest of this app (`pace5000_app.py`, `pace5000_ui_main.py`, `pace5000_backend.py`, `pace5000_api.py`) by its fully-qualified package name, so it can also be embedded in a larger launcher (see `bl18c_controller/main.py`) without any duplicate-import surprises.

Connection settings (IP, port, COM port, baud rate) are saved automatically to `pace5000_settings.json`.
The last-used log save directory is also persisted in `pace5000_settings.json` and restored as the default on next launch.

## API

Only available when running `app.py` standalone (not when this app is embedded in another launcher). Open **API → Configure and start API** from the menu bar once connected and enable it there, or auto-start it with `--api` on the command line:

```
python apps/PACE5000/app.py --api --api-host 0.0.0.0 --api-port 8765 --api-key <key>
```

**Authentication**: binding to `127.0.0.1` (the default) requires no API key — only processes on the same machine can reach it. Binding to any other host (e.g. `0.0.0.0` or a specific LAN IP, to allow other machines on the network to reach it) requires an API key, sent as the `X-API-Key` header on every request. Generate one from the UI ("Regenerate") or pass `--api-key`; it is persisted in `pace5000_settings.json` so it stays stable across restarts.

All endpoints are under `/api/v1`. Request/response bodies are JSON.

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/health` | no | `{"ok": true}` — liveness check |
| GET | `/api/v1/status` | yes | Current pressure, target, slew rate, control mode, source pressures |
| POST | `/api/v1/pressure` | yes | Body `{"pressure": 2.0, "unit": "MPa", "rate": 0.2, "rate_unit": "MPa/min"}`. Sets the slew rate (verified) then the setpoint; returns immediately — does **not** wait for the pressure to arrive. `unit` is `"MPa"` or `"Bar"`; `rate_unit` is `"MPa/min"`, `"Bar/min"`, `"MPa/sec"`, or `"Bar/sec"`. 409 if the target exceeds the +ve source pressure, or the device's slew rate can't be verified. |
| POST | `/api/v1/control_mode` | yes | Body `{"enabled": true}` — toggle Control/Measure mode |
| GET | `/api/v1/pressure/wait` | yes | Query `tol`, `unit`, `timeout_s` (capped at 300 s). Blocks until the measured pressure is within `tol` of the current target, or returns 408 on timeout. For waits longer than a few tens of seconds, prefer polling `/status` yourself instead of holding this connection open. |

Example:

```bash
curl -H "X-API-Key: $KEY" -X POST http://127.0.0.1:8765/api/v1/pressure \
  -H "Content-Type: application/json" \
  -d '{"pressure": 2.0, "unit": "MPa", "rate": 0.2, "rate_unit": "MPa/min"}'

curl -H "X-API-Key: $KEY" http://127.0.0.1:8765/api/v1/status
```

**Implementation note**: pressure setting/waiting logic (slew-rate-before-setpoint ordering with read-back verification, and target-reached polling) lives in a single place — `Pace5000Backend.set_pressure_with_ramp()` / `wait_for_pressure()` — shared by this API, the Scheduled Control feature in this app, and the external `bl18c_controller` experimental scheduler that can embed this app. Sending the setpoint before the slew rate is confirmed risks the device approaching the new setpoint at whatever rate was previously in effect.

## Developer

Hiroki Kobayashi (https://orcid.org/0000-0002-3682-7558)