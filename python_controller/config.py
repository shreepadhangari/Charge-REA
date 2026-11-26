"""
Device configuration and constants for the OWON SPE6103 power supply.

All communication defaults and protocol timing parameters are derived from
reverse engineering `Owon\\DC Power Supply Control.exe` (`winApp.jar`),
`Owon\\ini.pro`, and `Owon\\conf\\pref.json`. Hardware operating ratings come
from the OWON SPE6103 specification (60 V, 10 A, 300 W).
"""

from dataclasses import dataclass
from typing import Optional, Tuple, List


# ──────────────────────────────────────────────────────────────────────
# Communication Settings (verified from winApp.jar / conf/pref.json)
# ──────────────────────────────────────────────────────────────────────

DEFAULT_BAUDRATE = 115200
DEFAULT_DATA_BITS = 8
DEFAULT_STOP_BITS = 1
DEFAULT_PARITY = "N"  # None
DEFAULT_TIMEOUT = 2.0  # seconds (ComAgent.open uses 2000 ms timeout)
DEFAULT_WRITE_TIMEOUT = 2.0  # seconds

# Command and response line framing (verified in ComAgent.sendCommand)
COMMAND_TERMINATOR = "\n"  # LF (ASCII 0x0A)
RESPONSE_TERMINATOR = b"\n"
MAX_RESPONSE_LENGTH = 1024  # bytes
INTER_COMMAND_DELAY = 0.100  # seconds (ComAgent.WriteDelay = 100 ms, ReadDelay = 100 ms)


# ──────────────────────────────────────────────────────────────────────
# USB Device Identification
# ──────────────────────────────────────────────────────────────────────

# ComAgent opens RXTX with owner tag "MyCommCH340", indicating a CH340 USB-UART
# Virtual COM Port bridge (QinHeng Electronics VID 0x1A86).
# Exact USB VID/PID on a host depends on the physical unit's bridge chip;
# auto-detection prioritizes QinHeng CH34x VID/PIDs first and verifies via *IDN?.
CH340_VENDOR_ID = 0x1A86

CH340_USB_VID_PID: List[Tuple[int, int]] = [
    (0x1A86, 0x7523),  # QinHeng CH340 / CH341 USB-SERIAL
    (0x1A86, 0x5523),  # QinHeng CH341 USB-SERIAL
    (0x1A86, 0x7522),  # QinHeng CH340K
    (0x1A86, 0x55D3),  # QinHeng CH343 USB-SERIAL
    (0x1A86, 0x55D4),  # QinHeng CH9102 USB-SERIAL
]

# Legacy alias retained for convenience
KNOWN_USB_VID_PID = CH340_USB_VID_PID

# Expected *IDN? fields
IDN_MANUFACTURER = "OWON"
IDN_MODEL_PREFIX = "SPE"


# ──────────────────────────────────────────────────────────────────────
# Hardware Limits — OWON SPE6103
# ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DeviceLimits:
    """
    Rated operating and protection limits for the OWON SPE6103 power supply.

    Note:
        In `winApp.jar` (`speConfig/Config.SPE6103`), the desktop GUI sets
        firmware/UI ceiling fields `MAXV = 61.0`, `MAXA = 10.1`, `MAXP = 312.0`
        so OVP (`VOLT:LIM`) and OCP (`CURR:LIM`) can be set marginally above the
        60.0 V / 10.0 A output ratings. Here, normal voltage/current/power
        setpoints strictly enforce the rated 60.0 V / 10.0 A / 300.0 W hardware
        limits, while OVP and OCP thresholds allow up to 61.0 V and 10.1 A as
        configured in `Config.SPE6103`.
    """

    model: str = "SPE6103"

    # Rated programmable output limits
    max_voltage: float = 60.0        # V
    min_voltage: float = 0.0         # V
    voltage_resolution: float = 0.001  # V (ComAgent DecimalFormat "0.000")

    max_current: float = 10.0        # A
    min_current: float = 0.0         # A
    current_resolution: float = 0.001  # A

    # Rated maximum output power (V * I must not exceed max_power)
    max_power: float = 300.0         # W

    # Protection limit ceilings from winApp.jar Config.SPE6103
    max_ovp: float = 61.0            # V (Config.SPE6103.MAXV)
    min_ovp: float = 0.0             # V
    max_ocp: float = 10.1            # A (Config.SPE6103.MAXA)
    min_ocp: float = 0.0             # A

    channels: int = 1


SPE6103_LIMITS = DeviceLimits()

KNOWN_MODELS = {
    "SPE6103": SPE6103_LIMITS,
}


@dataclass
class ConnectionConfig:
    """Configuration for connecting to the OWON SPE6103 power supply."""

    port: Optional[str] = None
    baudrate: int = DEFAULT_BAUDRATE
    timeout: float = DEFAULT_TIMEOUT
    write_timeout: float = DEFAULT_WRITE_TIMEOUT
    inter_command_delay: float = INTER_COMMAND_DELAY

    auto_detect: bool = True
    verify_identity: bool = True
    sync_on_connect: bool = True
    enter_remote_mode: bool = True
    fit_ma_protocol: bool = False  # Matches Global.isFitMaProtocol (MEASure:ALL? vs MEASure:ALL:INFO?)


@dataclass
class MonitorConfig:
    """Configuration for continuous measurement acquisition."""

    interval: float = 1.0
    csv_path: Optional[str] = None
    include_settings: bool = True
    include_protection: bool = True
    max_samples: Optional[int] = None
