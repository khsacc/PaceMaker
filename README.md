# PaceMaker

A PyQt6 desktop GUI for controlling and logging a **Druck PACE5000** pressure controller.

## Features

- Connect via **TCP/IP** (SCPI over port 5025) or **Serial (RS-232)**
- Set target pressure and slew rate; switch between Control / Measure modes
- Live pressure chart with configurable time window
- CSV data logging
- **Scheduled control**: build a sequence of pressure steps and waits, save/load as JSON, and run with live plot and automatic logging

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

```
python main.py
```

Connection settings (IP, port, COM port, baud rate) are saved automatically to `pace5000_settings.json`.
The last-used log save directory is also persisted in `pace5000_settings.json` and restored as the default on next launch.


## Developer

Hiroki Kobayashi (https://orcid.org/0000-0002-3682-7558)