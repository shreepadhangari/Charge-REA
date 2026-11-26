"""
Verified ASCII/SCPI protocol layer for the OWON SPE6103 power supply.

Every command string and response parser in this module is directly derived from
disassembly of `winApp.jar` inside `Owon\\DC Power Supply Control.exe`:
    - `delegate.serial.ComAgent`
    - `service.SpeParamService` ($1..$7)
    - `control.func.ChannelSettingController`
    - `storeroom.util.KeyPressListenFxUtil` ($1)
    - `model.MemoryBean`

Verified Serial Protocol:
    - Transport: USB-to-UART Virtual COM Port ("MyCommCH340"), 115200 baud, 8N1
    - Framing: ASCII commands terminated with LF ('\\n'), responses LF-terminated
    - Timing: ~100 ms command pacing (ComAgent WriteDelay/ReadDelay = 100 ms)

Verified Commands:
    *IDN?                   -> Manufacturer,Model,Serial,FirmwareVersion
    OUTP ON / OUTP OFF      -> Enable / disable DC output terminals
    OUTP?                   -> Query output state ("ON" / "OFF")
    VOLT <value>            -> Set output voltage setpoint (V)
    VOLT?                   -> Query voltage setpoint (V)
    CURR <value>            -> Set programmable current limit (A)
    CURR?                   -> Query programmable current limit (A)
    VOLT:LIM <value>        -> Set OVP threshold (V)
    VOLT:LIM?               -> Query OVP threshold (V)
    CURR:LIM <value>        -> Set OCP threshold (A)
    CURR:LIM?               -> Query OCP threshold (A)
    MEASure:ALL:INFO?       -> V,I,P,OVP(ON/OFF),OCP(ON/OFF),OTP(ON/OFF),Mode(0..3)
    MEASure:ALL?            -> Legacy FitMa variant: V,I,P,OVP(ON/OFF),OCP(ON/OFF),OTP(ON/OFF)
    SYSTem:REMote           -> Enter remote control mode (sent after init sync)
    SYSTem:LOCal            -> Return to local control mode (sent on disconnect)
"""

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from python_controller.exceptions import ResponseError

logger = logging.getLogger(__name__)


class SCPICommand:
    """Verified ASCII/SCPI commands extracted from OWON `winApp.jar`."""

    # Identification
    IDN_QUERY = "*IDN?"

    # Output control (ChannelSettingController / SpeParamService$1)
    OUTPUT_ON = "OUTP ON"
    OUTPUT_OFF = "OUTP OFF"
    OUTPUT_QUERY = "OUTP?"

    # Voltage setpoint (KeyPressListenFxUtil / SpeParamService$2 / MemoryBean)
    VOLTAGE_SET = "VOLT {value}"
    VOLTAGE_QUERY = "VOLT?"

    # Programmable current limit (KeyPressListenFxUtil / SpeParamService$3 / MemoryBean)
    CURRENT_SET = "CURR {value}"
    CURRENT_QUERY = "CURR?"

    # Independent Over-Voltage Protection threshold (VOLT:LIM)
    OVP_SET = "VOLT:LIM {value}"
    OVP_QUERY = "VOLT:LIM?"

    # Independent Over-Current Protection threshold (CURR:LIM)
    OCP_SET = "CURR:LIM {value}"
    OCP_QUERY = "CURR:LIM?"

    # Combined telemetry + protection + operating mode polling (SpeParamService$7)
    MEASURE_ALL_INFO = "MEASure:ALL:INFO?"
    MEASURE_ALL_MA = "MEASure:ALL?"

    # Remote / local front-panel lock control (ComAgent)
    SYSTEM_REMOTE = "SYSTem:REMote"
    SYSTEM_LOCAL = "SYSTem:LOCal"


@dataclass
class DeviceIdentity:
    """Parsed *IDN? device identification information."""

    manufacturer: str
    model: str
    serial_number: str
    firmware_version: str
    raw_response: str

    def __str__(self) -> str:
        return (
            f"{self.manufacturer} {self.model} "
            f"(S/N: {self.serial_number}, FW: {self.firmware_version})"
        )


class OutputState(Enum):
    """Power supply DC output terminal state."""

    OFF = 0
    ON = 1


class OperatingMode(Enum):
    """
    Operating state code from field [6] of `MEASure:ALL:INFO?`
    (`SpeParamService$7` & `strings_en.properties`):
        "0" -> Standby
        "1" -> CV (Constant Voltage)
        "2" -> CC (Constant Current)
        "3" -> Fault
    """

    STANDBY = "Standby"
    CV = "Constant Voltage"
    CC = "Constant Current"
    FAULT = "Fault"
    UNKNOWN = "Unknown"


_MODE_CODE_MAP = {
    "0": OperatingMode.STANDBY,
    "1": OperatingMode.CV,
    "2": OperatingMode.CC,
    "3": OperatingMode.FAULT,
}


@dataclass
class MeasureAllResult:
    """
    Parsed response from `MEASure:ALL:INFO?` (7 fields) or `MEASure:ALL?` (6 fields).
    """

    measured_voltage: float
    measured_current: float
    measured_power: float
    ovp_tripped: bool
    ocp_tripped: bool
    otp_tripped: bool
    operating_mode: OperatingMode = OperatingMode.UNKNOWN
    raw_response: str = ""

    @property
    def fault_active(self) -> bool:
        """True if any hardware protection flag is tripped or mode is Fault."""
        return (
            self.ovp_tripped
            or self.ocp_tripped
            or self.otp_tripped
            or self.operating_mode == OperatingMode.FAULT
        )

    @property
    def fault_summary(self) -> str:
        """Comma-separated summary of active protection/fault indicators."""
        flags = []
        if self.ovp_tripped:
            flags.append("OVP")
        if self.ocp_tripped:
            flags.append("OCP")
        if self.otp_tripped:
            flags.append("OTP")
        if not flags and self.operating_mode == OperatingMode.FAULT:
            flags.append("FAULT")
        return ", ".join(flags)


@dataclass
class DeviceStatus:
    """Comprehensive snapshot of settings, measurements, protection, and faults."""

    output_state: OutputState
    set_voltage: float
    set_current: float
    measured_voltage: float
    measured_current: float
    measured_power: float
    ovp_threshold: Optional[float] = None
    ocp_threshold: Optional[float] = None
    operating_mode: OperatingMode = OperatingMode.UNKNOWN
    ovp_tripped: bool = False
    ocp_tripped: bool = False
    otp_tripped: bool = False

    @property
    def is_output_on(self) -> bool:
        return self.output_state == OutputState.ON

    @property
    def fault_active(self) -> bool:
        return (
            self.ovp_tripped
            or self.ocp_tripped
            or self.otp_tripped
            or self.operating_mode == OperatingMode.FAULT
        )

    @property
    def fault_summary(self) -> str:
        flags = []
        if self.ovp_tripped:
            flags.append("OVP")
        if self.ocp_tripped:
            flags.append("OCP")
        if self.otp_tripped:
            flags.append("OTP")
        if not flags and self.operating_mode == OperatingMode.FAULT:
            flags.append("FAULT")
        return ", ".join(flags)

    def __str__(self) -> str:
        state = "ON" if self.is_output_on else "OFF"
        lines = [
            f"Output:   {state} ({self.operating_mode.value})",
            f"Set:      {self.set_voltage:.3f} V / {self.set_current:.3f} A",
            f"Measured: {self.measured_voltage:.3f} V / {self.measured_current:.3f} A",
            f"Power:    {self.measured_power:.3f} W",
        ]
        if self.ovp_threshold is not None:
            lines.append(f"OVP:      {self.ovp_threshold:.3f} V (tripped={self.ovp_tripped})")
        if self.ocp_threshold is not None:
            lines.append(f"OCP:      {self.ocp_threshold:.3f} A (tripped={self.ocp_tripped})")
        if self.otp_tripped:
            lines.append("OTP:      TRIPPED")
        if self.fault_active:
            lines.append(f"Fault:    {self.fault_summary}")
        return "\n".join(lines)


class ProtocolHandler:
    """Encodes verified OWON SPE6103 commands and decodes responses."""

    # ── Command Builders ──────────────────────────────────────────

    @staticmethod
    def build_identify() -> str:
        return SCPICommand.IDN_QUERY

    @staticmethod
    def build_output_on() -> str:
        return SCPICommand.OUTPUT_ON

    @staticmethod
    def build_output_off() -> str:
        return SCPICommand.OUTPUT_OFF

    @staticmethod
    def build_output_query() -> str:
        return SCPICommand.OUTPUT_QUERY

    @staticmethod
    def build_set_voltage(voltage: float) -> str:
        return SCPICommand.VOLTAGE_SET.format(value=f"{voltage:.3f}")

    @staticmethod
    def build_query_voltage() -> str:
        return SCPICommand.VOLTAGE_QUERY

    @staticmethod
    def build_set_current(current: float) -> str:
        return SCPICommand.CURRENT_SET.format(value=f"{current:.3f}")

    @staticmethod
    def build_query_current() -> str:
        return SCPICommand.CURRENT_QUERY

    @staticmethod
    def build_set_ovp(voltage: float) -> str:
        return SCPICommand.OVP_SET.format(value=f"{voltage:.3f}")

    @staticmethod
    def build_query_ovp() -> str:
        return SCPICommand.OVP_QUERY

    @staticmethod
    def build_set_ocp(current: float) -> str:
        return SCPICommand.OCP_SET.format(value=f"{current:.3f}")

    @staticmethod
    def build_query_ocp() -> str:
        return SCPICommand.OCP_QUERY

    @staticmethod
    def build_measure_all(fit_ma: bool = False) -> str:
        return (
            SCPICommand.MEASURE_ALL_MA
            if fit_ma
            else SCPICommand.MEASURE_ALL_INFO
        )

    @staticmethod
    def build_system_remote() -> str:
        return SCPICommand.SYSTEM_REMOTE

    @staticmethod
    def build_system_local() -> str:
        return SCPICommand.SYSTEM_LOCAL

    # ── Response Parsers ──────────────────────────────────────────

    @staticmethod
    def parse_identity(response: str) -> DeviceIdentity:
        """
        Parse `*IDN?` response (`SpeParamService$6` splits on ',').
        Expected format: `<Manufacturer>,<Model>,<SerialNumber>,<FirmwareVersion>`
        """
        cleaned = response.strip()
        if not cleaned:
            raise ResponseError("Empty *IDN? response.", raw_response=response)

        parts = [p.strip() for p in cleaned.split(",")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise ResponseError(
                f"Malformed *IDN? response (expected comma-separated fields): {response!r}",
                raw_response=response,
            )

        manufacturer = parts[0]
        model = parts[1]
        serial_number = parts[2] if len(parts) >= 3 and parts[2] else "unknown"
        firmware = parts[3] if len(parts) >= 4 and parts[3] else "unknown"

        return DeviceIdentity(
            manufacturer=manufacturer,
            model=model,
            serial_number=serial_number,
            firmware_version=firmware,
            raw_response=cleaned,
        )

    @staticmethod
    def parse_float(response: str) -> float:
        """Parse a scalar ASCII numeric response into a float."""
        cleaned = response.strip() if response is not None else ""
        if not cleaned:
            raise ResponseError(
                f"Empty numeric response: {response!r}",
                raw_response=str(response or ""),
            )
        match = re.fullmatch(
            r"([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)", cleaned
        )
        if not match:
            raise ResponseError(
                f"Cannot parse numeric response: {response!r}",
                raw_response=response,
            )
        return float(match.group(1))

    @staticmethod
    def parse_flag(token: str) -> bool:
        """Parse 'ON'/'OFF' (or '1'/'0') protection/output token."""
        cleaned = token.strip().upper()
        if cleaned in ("ON", "1", "TRUE"):
            return True
        if cleaned in ("OFF", "0", "FALSE"):
            return False
        raise ResponseError(
            f"Invalid boolean/state token: {token!r}",
            raw_response=token,
        )

    @classmethod
    def parse_output_state(cls, response: str) -> OutputState:
        """
        Parse an `OUTP?` response.
        `SpeParamService$1` expects 'ON'/'OFF'; '1'/'0' also supported.
        """
        cleaned = response.strip() if response is not None else ""
        if not cleaned:
            raise ResponseError("Empty OUTP? response.", raw_response="")
        is_on = cls.parse_flag(cleaned)
        return OutputState.ON if is_on else OutputState.OFF

    @classmethod
    def parse_measure_all(
        cls, response: str, fit_ma: bool = False
    ) -> MeasureAllResult:
        """
        Parse `MEASure:ALL:INFO?` or `MEASure:ALL?` response (`SpeParamService$7`).

        Expected fields:
            [0] measured_voltage (float)
            [1] measured_current (float)
            [2] measured_power (float)
            [3] OVP tripped ("ON"/"OFF")
            [4] OCP tripped ("ON"/"OFF")
            [5] OTP tripped ("ON"/"OFF")
            [6] operating mode ("0"=Standby, "1"=CV, "2"=CC, "3"=Fault) [when present]
        """
        cleaned = response.strip() if response is not None else ""
        parts = [p.strip() for p in cleaned.split(",")] if cleaned else []

        if len(parts) < 6:
            raise ResponseError(
                f"Expected at least 6 comma-separated fields from MEASure:ALL response, "
                f"got {len(parts)}: {response!r}",
                raw_response=response or "",
            )

        try:
            meas_v = cls.parse_float(parts[0])
            meas_i = cls.parse_float(parts[1])
            meas_p = cls.parse_float(parts[2])
            ovp_tripped = cls.parse_flag(parts[3])
            ocp_tripped = cls.parse_flag(parts[4])
            otp_tripped = cls.parse_flag(parts[5])
        except ResponseError as exc:
            raise ResponseError(
                f"Malformed MEASure:ALL payload {response!r}: {exc}",
                raw_response=response or "",
            ) from exc

        mode = OperatingMode.UNKNOWN
        if len(parts) >= 7 and parts[6] != "":
            mode_code = parts[6]
            if mode_code in _MODE_CODE_MAP:
                mode = _MODE_CODE_MAP[mode_code]
            elif not fit_ma:
                raise ResponseError(
                    f"Unknown operating mode code {mode_code!r} in {response!r}",
                    raw_response=response or "",
                )

        return MeasureAllResult(
            measured_voltage=meas_v,
            measured_current=meas_i,
            measured_power=meas_p,
            ovp_tripped=ovp_tripped,
            ocp_tripped=ocp_tripped,
            otp_tripped=otp_tripped,
            operating_mode=mode,
            raw_response=cleaned,
        )
