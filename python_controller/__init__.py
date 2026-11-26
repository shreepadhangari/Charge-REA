"""
OWON SPE6103 Programmable DC Power Supply Controller.

Independent Python library, monitor, and CLI derived from reverse engineering
`Owon\\DC Power Supply Control.exe` (`winApp.jar`).
"""

__version__ = "1.0.0"

from python_controller.config import ConnectionConfig, DeviceLimits, MonitorConfig, SPE6103_LIMITS
from python_controller.exceptions import (
    CommandError,
    CommunicationError,
    ConnectionError,
    DeviceError,
    OwonError,
    ResponseError,
    SafetyError,
    TimeoutError,
)
from python_controller.monitor import Measurement, Monitor
from python_controller.power_supply import PowerSupply
from python_controller.protocol import (
    DeviceIdentity,
    DeviceStatus,
    MeasureAllResult,
    OperatingMode,
    OutputState,
    ProtocolHandler,
    SCPICommand,
)

__all__ = [
    "PowerSupply",
    "Monitor",
    "Measurement",
    "ConnectionConfig",
    "MonitorConfig",
    "DeviceLimits",
    "SPE6103_LIMITS",
    "ProtocolHandler",
    "SCPICommand",
    "DeviceIdentity",
    "DeviceStatus",
    "MeasureAllResult",
    "OutputState",
    "OperatingMode",
    "OwonError",
    "CommunicationError",
    "ConnectionError",
    "CommandError",
    "ResponseError",
    "TimeoutError",
    "SafetyError",
    "DeviceError",
]
