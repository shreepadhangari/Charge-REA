"""
High-level control interface for the OWON SPE6103 programmable DC power supply.

Safety & Protocol Alignment:
    - Connects without EVER automatically enabling DC output.
    - Synchronizes settings via verified `winApp.jar` queries (`OUTP?`, `VOLT?`,
      `CURR?`, `VOLT:LIM?`, `CURR:LIM?`, `*IDN?`) and enters `SYSTem:REMote`.
    - Strictly enforces 60.0 V, 10.0 A, and 300.0 W rated capabilities on
      setpoints (`set_voltage`, `set_current_limit`, `configure`) AND before `output_on()`.
    - Keeps programmable current limit (`CURR`) strictly separate from independent
      over-current protection (`CURR:LIM`), and voltage setpoint (`VOLT`) separate
      from independent over-voltage protection (`VOLT:LIM`).
    - Safe shutdown returns to `SYSTem:LOCal` and disables output by default.
"""

import logging
import threading
from typing import Optional

from python_controller.config import (
    ConnectionConfig,
    DeviceLimits,
    KNOWN_MODELS,
    SPE6103_LIMITS,
)
from python_controller.exceptions import (
    CommunicationError,
    ConnectionError,
    ResponseError,
    SafetyError,
    TimeoutError,
)
from python_controller.protocol import (
    DeviceIdentity,
    DeviceStatus,
    MeasureAllResult,
    OutputState,
    ProtocolHandler,
)
from python_controller.transport import SerialTransport

logger = logging.getLogger(__name__)


class PowerSupply:
    """Thread-safe, safety-validated high-level controller for OWON SPE6103."""

    def __init__(
        self,
        port: Optional[str] = None,
        config: Optional[ConnectionConfig] = None,
        limits: Optional[DeviceLimits] = None,
    ):
        if config is None:
            config = ConnectionConfig(port=port)
        elif port is not None and not config.port:
            config.port = port

        self._config = config
        self._limits = limits or SPE6103_LIMITS
        self._transport = SerialTransport(
            port=self._config.port,
            baudrate=self._config.baudrate,
            timeout=self._config.timeout,
            write_timeout=self._config.write_timeout,
            inter_command_delay=self._config.inter_command_delay,
        )
        self._protocol = ProtocolHandler()
        self._api_lock = threading.RLock()
        self._identity: Optional[DeviceIdentity] = None
        self._connected = False

    # ── Connection Lifecycle ──────────────────────────────────────

    def connect(self) -> str:
        """
        Connect to the power supply without enabling output.

        Mirrors `ComAgent.initRequest()` / `accumulateSyncCount()`:
        verifies `*IDN?` (when `verify_identity=True`) and switches instrument to
        `SYSTem:REMote` (when `enter_remote_mode=True`).
        """
        with self._api_lock:
            port = self._transport.connect()
            self._connected = True

            try:
                if self._config.verify_identity:
                    identity = self.identify()
                    logger.info("Connected to %s", identity)
                    model_key = identity.model.upper()
                    if model_key in KNOWN_MODELS:
                        self._limits = KNOWN_MODELS[model_key]
                    else:
                        logger.warning(
                            "Model %s not in KNOWN_MODELS; keeping SPE6103 safety limits.",
                            model_key,
                        )

                if self._config.enter_remote_mode:
                    self.set_remote_mode()
            except (CommunicationError, TimeoutError, ResponseError) as exc:
                logger.warning("Connect handshake partially skipped/failed: %s", exc)

            return port

    def disconnect(self, safe_shutdown: bool = True) -> None:
        """
        Close the connection safely.

        Args:
            safe_shutdown: If True (default), send `OUTP OFF` before `SYSTem:LOCal`.
                           Note: Turning output OFF via serial does not guarantee
                           instantaneous physical discharge of output capacitance.
        """
        with self._api_lock:
            if self._connected:
                if safe_shutdown:
                    try:
                        self.output_off()
                        logger.info("Safe shutdown: OUTP OFF sent before disconnect.")
                    except Exception as exc:
                        logger.warning(
                            "Could not send OUTP OFF before disconnect: %s", exc
                        )
                try:
                    self.set_local_mode()
                except Exception as exc:
                    logger.debug("Could not send SYSTem:LOCal before disconnect: %s", exc)

            self._transport.disconnect()
            self._connected = False
            self._identity = None

    @property
    def is_connected(self) -> bool:
        return self._connected and self._transport.is_connected

    @property
    def limits(self) -> DeviceLimits:
        return self._limits

    @property
    def identity(self) -> Optional[DeviceIdentity]:
        return self._identity

    # ── Identification ────────────────────────────────────────────

    def identify(self) -> DeviceIdentity:
        """Query `*IDN?` and cache `DeviceIdentity`."""
        with self._api_lock:
            resp = self._send_command(self._protocol.build_identify())
            assert resp is not None
            identity = self._protocol.parse_identity(resp)
            self._identity = identity
            return identity

    # ── Setpoints (Voltage & Programmable Current Limit) ──────────

    def set_voltage(self, voltage: float) -> None:
        """
        Set target output voltage (`VOLT <v>`).

        Enforces both voltage rating (0..60 V) and 300 W combined power budget
        against the device's active programmable current limit (`CURR?`).
        """
        with self._api_lock:
            self._validate_voltage(voltage)
            current_limit = self.get_current_limit()
            self._validate_power(voltage, current_limit)
            cmd = self._protocol.build_set_voltage(voltage)
            self._send_command(cmd, expect_response=False)
            logger.info("Voltage setpoint set to %.3f V", voltage)

    def get_voltage(self) -> float:
        """Query voltage setpoint (`VOLT?`)."""
        with self._api_lock:
            resp = self._send_command(self._protocol.build_query_voltage())
            assert resp is not None
            return self._protocol.parse_float(resp)

    def set_current_limit(self, current: float) -> None:
        """
        Set programmable constant-current limit (`CURR <i>`).

        Distinct from independent Over-Current Protection (`set_ocp` / `CURR:LIM`).
        Enforces current rating (0..10 A) and 300 W combined power budget against
        the device's active voltage setpoint (`VOLT?`).
        """
        with self._api_lock:
            self._validate_current(current)
            voltage = self.get_voltage()
            self._validate_power(voltage, current)
            cmd = self._protocol.build_set_current(current)
            self._send_command(cmd, expect_response=False)
            logger.info("Programmable current limit set to %.3f A", current)

    def get_current_limit(self) -> float:
        """Query programmable current limit (`CURR?`)."""
        with self._api_lock:
            resp = self._send_command(self._protocol.build_query_current())
            assert resp is not None
            return self._protocol.parse_float(resp)

    def configure(self, voltage: float, current_limit: float) -> None:
        """
        Safely configure both voltage setpoint and programmable current limit together.

        Validates individual limits AND `voltage * current_limit <= 300 W` first,
        then orders writes so the intermediate (V, I) state never violates the
        300 W budget (lowering whichever parameter decreases power first when needed).
        """
        with self._api_lock:
            self._validate_voltage(voltage)
            self._validate_current(current_limit)
            self._validate_power(voltage, current_limit)

            curr_v = self.get_voltage()
            curr_i = self.get_current_limit()

            if voltage * curr_i > self._limits.max_power:
                # Reduce current first so intermediate V * I stays within 300 W
                self._send_command(
                    self._protocol.build_set_current(current_limit),
                    expect_response=False,
                )
                self._send_command(
                    self._protocol.build_set_voltage(voltage),
                    expect_response=False,
                )
            elif curr_v * current_limit > self._limits.max_power:
                # Reduce voltage first
                self._send_command(
                    self._protocol.build_set_voltage(voltage),
                    expect_response=False,
                )
                self._send_command(
                    self._protocol.build_set_current(current_limit),
                    expect_response=False,
                )
            else:
                self._send_command(
                    self._protocol.build_set_voltage(voltage),
                    expect_response=False,
                )
                self._send_command(
                    self._protocol.build_set_current(current_limit),
                    expect_response=False,
                )

            logger.info(
                "Configured output to %.3f V / %.3f A (%.2f W ceiling)",
                voltage,
                current_limit,
                voltage * current_limit,
            )

    # ── Independent Hardware Protection (OVP / OCP) ───────────────

    def set_ovp(self, voltage: float) -> None:
        """Set independent Over-Voltage Protection threshold (`VOLT:LIM <v>`)."""
        with self._api_lock:
            self._validate_ovp(voltage)
            cmd = self._protocol.build_set_ovp(voltage)
            self._send_command(cmd, expect_response=False)
            logger.info("OVP threshold (VOLT:LIM) set to %.3f V", voltage)

    def get_ovp(self) -> float:
        """Query Over-Voltage Protection threshold (`VOLT:LIM?`)."""
        with self._api_lock:
            resp = self._send_command(self._protocol.build_query_ovp())
            assert resp is not None
            return self._protocol.parse_float(resp)

    def set_ocp(self, current: float) -> None:
        """Set independent Over-Current Protection threshold (`CURR:LIM <i>`)."""
        with self._api_lock:
            self._validate_ocp(current)
            cmd = self._protocol.build_set_ocp(current)
            self._send_command(cmd, expect_response=False)
            logger.info("OCP threshold (CURR:LIM) set to %.3f A", current)

    def get_ocp(self) -> float:
        """Query Over-Current Protection threshold (`CURR:LIM?`)."""
        with self._api_lock:
            resp = self._send_command(self._protocol.build_query_ocp())
            assert resp is not None
            return self._protocol.parse_float(resp)

    # ── Measurements & Telemetry (`MEASure:ALL:INFO?`) ────────────

    def measure_all(self) -> MeasureAllResult:
        """
        Query combined live measurements, trip flags, and operating mode via
        `MEASure:ALL:INFO?` (or `MEASure:ALL?` when `fit_ma_protocol=True`).
        """
        with self._api_lock:
            cmd = self._protocol.build_measure_all(
                fit_ma=self._config.fit_ma_protocol
            )
            resp = self._send_command(cmd)
            assert resp is not None
            return self._protocol.parse_measure_all(
                resp, fit_ma=self._config.fit_ma_protocol
            )

    def measure_voltage(self) -> float:
        """Measure actual output voltage (V) from `MEASure:ALL:INFO?`."""
        return self.measure_all().measured_voltage

    def measure_current(self) -> float:
        """Measure actual output current (A) from `MEASure:ALL:INFO?`."""
        return self.measure_all().measured_current

    def measure_power(self) -> float:
        """Measure actual output power (W) from `MEASure:ALL:INFO?`."""
        return self.measure_all().measured_power

    # ── Output Control ────────────────────────────────────────────

    def output_on(self) -> None:
        """
        Explicitly enable DC output terminals (`OUTP ON`).

        Safety checks before enabling:
            1. Queries active `VOLT?` and `CURR?` and validates V * I <= 300 W.
            2. Refuses to enable if communication fails or power exceeds 300 W.
        """
        with self._api_lock:
            v = self.get_voltage()
            i = self.get_current_limit()
            self._validate_voltage(v)
            self._validate_current(i)
            self._validate_power(v, i)
            self._send_command(self._protocol.build_output_on(), expect_response=False)
            logger.info("DC OUTPUT ENABLED (setpoint %.3f V / %.3f A)", v, i)

    def output_off(self) -> None:
        """
        Disable DC output terminals (`OUTP OFF`).

        Note: Disabling the output stage does NOT guarantee immediate zero
        voltage across external/internal capacitance; verify via `measure_voltage()`.
        """
        with self._api_lock:
            self._send_command(self._protocol.build_output_off(), expect_response=False)
            logger.info("DC OUTPUT DISABLED")

    def get_output_state(self) -> OutputState:
        """Query current output state (`OUTP?`)."""
        with self._api_lock:
            resp = self._send_command(self._protocol.build_output_query())
            assert resp is not None
            return self._protocol.parse_output_state(resp)

    def is_output_on(self) -> bool:
        return self.get_output_state() == OutputState.ON

    # ── Comprehensive Status & Remote/Local ───────────────────────

    def get_status(self) -> DeviceStatus:
        """
        Query a complete status snapshot combining `OUTP?`, `VOLT?`, `CURR?`,
        `VOLT:LIM?`, `CURR:LIM?`, and `MEASure:ALL:INFO?`.
        """
        with self._api_lock:
            output_state = self.get_output_state()
            set_v = self.get_voltage()
            set_i = self.get_current_limit()
            ovp = self.get_ovp()
            ocp = self.get_ocp()
            meas = self.measure_all()

            return DeviceStatus(
                output_state=output_state,
                set_voltage=set_v,
                set_current=set_i,
                measured_voltage=meas.measured_voltage,
                measured_current=meas.measured_current,
                measured_power=meas.measured_power,
                ovp_threshold=ovp,
                ocp_threshold=ocp,
                operating_mode=meas.operating_mode,
                ovp_tripped=meas.ovp_tripped,
                ocp_tripped=meas.ocp_tripped,
                otp_tripped=meas.otp_tripped,
            )

    def set_remote_mode(self) -> None:
        """Lock/enable PC remote control mode (`SYSTem:REMote`)."""
        with self._api_lock:
            self._send_command(
                self._protocol.build_system_remote(), expect_response=False
            )

    def set_local_mode(self) -> None:
        """Return front-panel buttons to local control (`SYSTem:LOCal`)."""
        with self._api_lock:
            self._send_command(
                self._protocol.build_system_local(), expect_response=False
            )

    def raw_command(
        self, command: str, expect_response: Optional[bool] = None
    ) -> Optional[str]:
        """Send an explicit raw ASCII/SCPI command string."""
        if expect_response is None:
            expect_response = command.strip().endswith("?")
        with self._api_lock:
            return self._send_command(command, expect_response=expect_response)

    # ── Limit Validators ──────────────────────────────────────────

    def _validate_voltage(self, voltage: float) -> None:
        if voltage < self._limits.min_voltage or voltage > self._limits.max_voltage:
            raise SafetyError(
                f"Voltage {voltage:.3f} V is outside allowed range "
                f"[{self._limits.min_voltage:.3f}, {self._limits.max_voltage:.3f}] V.",
                parameter="voltage",
                requested_value=voltage,
                limit_value=self._limits.max_voltage,
            )

    def _validate_current(self, current: float) -> None:
        if current < self._limits.min_current or current > self._limits.max_current:
            raise SafetyError(
                f"Current limit {current:.3f} A is outside allowed range "
                f"[{self._limits.min_current:.3f}, {self._limits.max_current:.3f}] A.",
                parameter="current",
                requested_value=current,
                limit_value=self._limits.max_current,
            )

    def _validate_power(self, voltage: float, current: float) -> None:
        power = voltage * current
        if power > self._limits.max_power + 1e-6:
            raise SafetyError(
                f"Requested power {power:.2f} W ({voltage:.3f} V x {current:.3f} A) "
                f"exceeds maximum rated power {self._limits.max_power:.0f} W.",
                parameter="power",
                requested_value=power,
                limit_value=self._limits.max_power,
            )

    def _validate_ovp(self, voltage: float) -> None:
        if voltage < self._limits.min_ovp or voltage > self._limits.max_ovp:
            raise SafetyError(
                f"OVP threshold {voltage:.3f} V is outside allowed range "
                f"[{self._limits.min_ovp:.3f}, {self._limits.max_ovp:.3f}] V.",
                parameter="ovp",
                requested_value=voltage,
                limit_value=self._limits.max_ovp,
            )

    def _validate_ocp(self, current: float) -> None:
        if current < self._limits.min_ocp or current > self._limits.max_ocp:
            raise SafetyError(
                f"OCP threshold {current:.3f} A is outside allowed range "
                f"[{self._limits.min_ocp:.3f}, {self._limits.max_ocp:.3f}] A.",
                parameter="ocp",
                requested_value=current,
                limit_value=self._limits.max_ocp,
            )

    # ── Internal I/O Helper ───────────────────────────────────────

    def _send_command(
        self, command: str, expect_response: bool = True
    ) -> Optional[str]:
        if not self.is_connected:
            raise ConnectionError("Not connected to power supply.")
        return self._transport.send_command(
            command, expect_response=expect_response
        )

    # ── Context Manager ───────────────────────────────────────────

    def __enter__(self) -> "PowerSupply":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.disconnect(safe_shutdown=True)
        return False

    def __repr__(self) -> str:
        status = "connected" if self.is_connected else "disconnected"
        model = self._identity.model if self._identity else self._limits.model
        return f"PowerSupply(model={model}, port={self._transport.port!r}, status={status})"
