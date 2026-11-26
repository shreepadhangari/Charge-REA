# OWON SPE6103 Independent Python Controller

An independent, cross-platform (Windows & **Raspberry Pi 5 / Raspberry Pi OS**) Python control library, thread-safe continuous monitor, and CLI for the **OWON SPE6103** programmable DC power supply (`60 V` / `10 A` / `300 W`), built directly from reverse-engineering [`Owon/DC Power Supply Control.exe`](<file:///e:/Project/BMS/Major/Charge%20REA/Owon/DC%20Power%20Supply%20Control.exe>) (`winApp.jar`).

The original vendor application in [`Owon/`](<file:///e:/Project/BMS/Major/Charge%20REA/Owon>) remains completely unmodified for reference.

---

## Architecture Overview

```text
Charge REA/
├── Owon/                            # Original vendor software (untouched; Windows ref only)
│   ├── DC Power Supply Control.exe  # exe4j Java 8/JavaFX launcher (115200 8N1 serial)
│   ├── ini.pro                      # protocol= com
│   └── conf/pref.json               # 115200, 8, 1, None, isFitMaProtocol=false
├── python_controller/               # Pure-Python cross-platform package (Windows + Linux/Pi OS)
│   ├── __init__.py                  # Public exports
│   ├── config.py                    # Verified 115200 8N1 defaults, 100 ms pacing, SPE6103 limits
│   ├── exceptions.py                # OwonError, CommunicationError, SafetyError, DeviceError, ...
│   ├── protocol.py                  # Verified ASCII/SCPI builders & parsers (incl. MEASure:ALL:INFO?)
│   ├── transport.py                 # Thread-safe PySerial transport, flock locking & CH34x auto-detect
│   ├── power_supply.py              # High-level PowerSupply API with 300 W budget enforcement
│   ├── monitor.py                   # Background Monitor with timestamps, fault capture & CSV export
│   └── cli.py                       # CLI parser & SIGINT/SIGTERM safe shutdown logic
├── main.py                          # CLI entry point (#!/usr/bin/env python3, LF line endings)
├── pyproject.toml                   # Packaging metadata & `owon-psu` script entry point
├── tests/                           # Unit tests (mocked serial transport, incl. Linux/Pi paths)
│   ├── test_protocol.py
│   ├── test_transport.py
│   ├── test_power_supply.py
│   └── test_monitor_and_cli.py
├── docs/
│   ├── reverse_engineering.md       # Static & JVM bytecode RE findings from winApp.jar
│   ├── protocol_reference.md        # Verified command/response tables vs unverified SCPI
│   └── hardware_setup.md            # Windows & Raspberry Pi 5 driver, dialout, venv & safety guide
└── requirements.txt
```

---

## Quick Start

### Windows

```powershell
pip install -r requirements.txt
python main.py --list-ports
```

### Raspberry Pi 5 (Raspberry Pi OS Bookworm / Trixie)

```bash
# 1. Grant serial port permission once (log out/in afterward):
sudo usermod -aG dialout "$USER"

# 2. Install dependencies inside a venv (recommended due to PEP 668):
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Or via Raspberry Pi OS apt instead of venv:
# sudo apt update && sudo apt install -y python3-serial python3-pytest

# 3. Verify port detection (typically /dev/ttyUSB0):
python3 main.py --list-ports
```

See [`docs/hardware_setup.md`](<file:///e:/Project/BMS/Major/Charge%20REA/docs/hardware_setup.md>) for `/dev/serial/by-id/...` persistent paths and `udev`/`systemd` tips.

### CLI Examples

```bash
# Detect serial ports / identify instrument
python3 main.py --list-ports
python3 main.py --identify
python3 main.py --port /dev/ttyUSB0 --status   # or --port COM3 on Windows

# Configure independent protection limits (VOLT:LIM, CURR:LIM)
python3 main.py --ovp 15 --ocp 2.5

# Configure output setpoint (VOLT, CURR) -- 300 W budget enforced
python3 main.py --voltage 12 --current 2

# Explicit output control (never auto-enabled on startup)
python3 main.py --output on
python3 main.py --output off

# Continuous monitoring with CSV logging (timestamps, setpoints, measurements, OVP/OCP/OTP faults)
python3 main.py --monitor --interval 1 --csv measurements.csv
```

### Python Library API

```python
from python_controller import PowerSupply
from python_controller.monitor import Monitor

# Pass "/dev/ttyUSB0" on Raspberry Pi OS, "COM3" on Windows, or omit to auto-detect
psu = PowerSupply(port=None)
psu.connect()                   # Enters SYSTem:REMote; output stays OFF

try:
    print(psu.identify())
    psu.set_ovp(15.0)           # VOLT:LIM 15.000
    psu.set_ocp(2.5)            # CURR:LIM 2.500
    psu.configure(12.0, 2.0)    # Safely sets VOLT 12.000 and CURR 2.000 (<= 300 W)
    psu.output_on()             # Checks 300 W budget, sends OUTP ON

    meas = psu.measure_all()    # Queries MEASure:ALL:INFO? (V, I, P, OVP/OCP/OTP trips, mode)
    print(meas.measured_voltage, meas.measured_current, meas.measured_power, meas.operating_mode)
    print(psu.get_status())
finally:
    psu.output_off()
    psu.disconnect()            # Sends OUTP OFF (if safe_shutdown=True) and SYSTem:LOCal
```

---

## Running Tests

```bash
python3 -m pytest -v tests
```

- **Verified by tests**: Mocked unit tests covering `ProtocolHandler`, `SerialTransport` (including Raspberry Pi `/dev/ttyUSB0`/`/dev/ttyACM0` selection, `/dev/ttyAMA*` exclusion, `exclusive=True`, and Linux `dialout` permission errors), `PowerSupply` `60 V` / `10 A` / `300 W` enforcement, `Monitor` CSV logging, and `CLI` (`SIGINT`/`SIGTERM`, safety validation).
- **Hardware validation status**: No physical serial COM port or Raspberry Pi 5 hardware was attached in this workspace environment; follow [`docs/hardware_setup.md`](<file:///e:/Project/BMS/Major/Charge%20REA/docs/hardware_setup.md>) Stages 2-5 on your Pi 5 with the SPE6103 connected.
