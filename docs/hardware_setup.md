# Hardware Setup, Dependencies, and Safety Guide

---

## 1. Python Environment Setup

### Prerequisites
- Python 3.9+ (tested on Windows Python 3.13.3; compatible with Raspberry Pi OS Bookworm Python 3.11 and Trixie Python 3.13)
- `pyserial >= 3.5` (serial transport)
- `pytest >= 7.0` (unit tests)

### Windows Installation
```powershell
pip install -r requirements.txt
```

### Raspberry Pi 5 / Raspberry Pi OS (Bookworm & Trixie) Installation

Modern Raspberry Pi OS (Bookworm / Trixie) enables PEP 668 (`externally-managed-environment`), so global `pip install` outside a virtual environment is blocked. Choose either approach:

**Option A -- Virtual Environment (Recommended):**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Optional: install `owon-psu` console command
pip install --no-build-isolation -e .
```

**Option B -- System Debian / Raspberry Pi OS `apt` Packages:**
```bash
sudo apt update
sudo apt install -y python3-serial python3-pytest
```

Verify available CLI options and inspect serial ports:
```bash
python3 main.py --help
python3 main.py --list-ports
```

---

## 2. USB Hardware & Driver Setup

1. Connect the OWON SPE6103 rear USB port to a PC or **Raspberry Pi 5 USB 2.0 / USB 3.0 Type-A port** using a standard USB data cable.
2. In `winApp.jar`, `ComAgent` opens the Virtual COM Port using handle tag `"MyCommCH340"`, indicating a CH340 USB-to-UART bridge (`VID 0x1A86`).
   - **Windows 10/11**: Windows Update typically installs the CH340 VCP driver automatically (appearing in Device Manager under **Ports (COM & LPT)** as `USB-SERIAL CH340 (COMx)`).
   - **Raspberry Pi 5 / Raspberry Pi OS**: The mainline Linux kernel includes the `ch341` driver (`cdc_acm` / `cp210x` / `ftdi_sio` are also built-in). The supply normally appears at `/dev/ttyUSB0` (or `/dev/ttyACM0`).
   - **Raspberry Pi 5 Internal UART Note**: Raspberry Pi 5 exposes internal hardware UARTs such as `/dev/ttyAMA10` / `/dev/ttyAMA0`. `SerialTransport` automatically skips internal non-USB UARTs during auto-detection and probes only USB serial devices, prioritizing QinHeng `0x1A86` (`CH340`/`CH341`/`CH343`/`CH9102`) first.
3. **Linux Serial Port Permissions (`dialout` group)**:
   ```bash
   sudo usermod -aG dialout "$USER"
   # Log out and back in (or reboot) for group membership to take effect
   ```
   To inspect attached USB ports or find a persistent path that survives USB re-plugging/reboots:
   ```bash
   ls -l /dev/ttyUSB* /dev/ttyACM* /dev/serial/by-id/*
   python3 main.py --port /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0 --identify
   ```
4. **Preventing Port Contention**:
   - Close OWON's vendor GUI (`DC Power Supply Control.exe`) on Windows, or any `minicom`/`screen`/controller instance on Linux. `SerialTransport` opens ports with `exclusive=True` (`fcntl.flock` on POSIX).
   - If another daemon such as `ModemManager` probes USB serial devices on custom Linux images, check `systemctl status ModemManager`.

If `--port` is omitted, `SerialTransport` prioritizes QinHeng CH34x VID/PID ports first and probes `*IDN?` looking for manufacturer `"OWON"`. To bypass auto-detection, pass `--port COM3` (Windows) or `--port /dev/ttyUSB0` (Raspberry Pi OS) explicitly.

---

## 3. Hardware Operating Limits & Power Budget

| Parameter | SPE6103 Limit Enforced by Python Controller | Notes |
|---|---|---|
| Output Voltage (`VOLT`) | `0.0` V .. `60.0` V | Strictly enforces rated output maximum |
| Programmable Current (`CURR`) | `0.0` A .. `10.0` A | Strictly enforces rated output maximum |
| Output Power Budget (`V * I`) | `<= 300.0` W | Enforced on `set_voltage()`, `set_current_limit()`, `configure()`, `output_on()`, and CLI validation |
| Over-Voltage Protection (`VOLT:LIM`) | `0.0` V .. `61.0` V | Ceiling matches `Config.SPE6103.MAXV` in `winApp.jar` |
| Over-Current Protection (`CURR:LIM`) | `0.0` A .. `10.1` A | Ceiling matches `Config.SPE6103.MAXA` in `winApp.jar` |

Notice: Setting `60 V` and `10 A` simultaneously (`600 W`) exceeds the `300 W` rating and raises `SafetyError` (CLI exit code `2`) **before** any serial command reaches the supply.

---

## 4. Safety Operating Procedures

1. **No Auto-Enable on Connect**: `psu.connect()` only queries `*IDN?` and sends `SYSTem:REMote`. DC terminals remain OFF until `psu.output_on()` or `--output on` is explicitly called.
2. **Discharge Warning After `OUTP OFF`**: Sending `OUTP OFF` disables the regulator stage, **not** passive output/load capacitance. Wait and confirm `measure_voltage()` drops to a safe level before disconnecting inductive, capacitive, or battery loads.
3. **Preserve Protection Thresholds**: Always configure `--ovp` (`VOLT:LIM`) slightly above target voltage and `--ocp` (`CURR:LIM`) above `--current` before energizing sensitive loads. Never disable built-in thermal (OTP), OVP, or OCP safeguards.
4. **Headless / SSH / `systemd` Safety on Raspberry Pi**: During `--monitor`, both `SIGINT` (Ctrl+C) and `SIGTERM` trigger monitor teardown, flush the CSV file, send `OUTP OFF`, and send `SYSTem:LOCal`.
5. **Sequential Testing Protocol on Real Hardware**:
   - **Stage 2 (Read-only)**: `python3 main.py --list-ports`, `--identify`, `--status`, `--measure`.
   - **Stage 3 (Low-power setpoint check)**: Set conservative values (`python3 main.py --voltage 5 --current 0.5`), verify via `--status`, then toggle `--output on` / `--output off` unloaded or with a safe resistive load.
   - **Stage 4 (Protection readback)**: Configure conservative limits (`python3 main.py --ovp 6 --ocp 0.6`), read back with `--status`. Do **not** induce real fault trips without current-limited/protected test fixtures.
   - **Stage 5 (Monitoring)**: Run `python3 main.py --monitor --interval 1 --csv measurements.csv --max-samples 10` and verify timestamps and CSV columns.

---

## 5. Validation Status

- **Unit Tests (Mocked Transport)**: Verified across unit tests in [`tests/`](file:///e:/Project/BMS/Major/Charge%20REA/tests) covering protocol, Windows & Linux (`/dev/ttyUSB*`, `/dev/ttyACM*`, skipping `/dev/ttyAMA*`, `dialout` permission diagnostics), exclusive locking, power budget safety, CSV monitoring, and `SIGINT`/`SIGTERM` shutdown.
- **Physical Hardware / Live Raspberry Pi 5 Run**: Not executed on this workstation (no physical COM ports attached, and this environment runs on Windows x86_64 rather than ARM64 Raspberry Pi 5).
