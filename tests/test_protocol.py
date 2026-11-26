"""
Unit tests for the verified OWON SPE6103 protocol layer (`python_controller.protocol`).
"""

import pytest

from python_controller.exceptions import ResponseError
from python_controller.protocol import (
    DeviceIdentity,
    DeviceStatus,
    MeasureAllResult,
    OperatingMode,
    OutputState,
    ProtocolHandler,
    SCPICommand,
)


class TestProtocolHandlerCommands:
    """Verify exact command strings matching `winApp.jar` disassembly."""

    def setup_method(self):
        self.handler = ProtocolHandler()

    def test_command_constants(self):
        assert SCPICommand.IDN_QUERY == "*IDN?"
        assert SCPICommand.OUTPUT_ON == "OUTP ON"
        assert SCPICommand.OUTPUT_OFF == "OUTP OFF"
        assert SCPICommand.OUTPUT_QUERY == "OUTP?"
        assert SCPICommand.VOLTAGE_QUERY == "VOLT?"
        assert SCPICommand.CURRENT_QUERY == "CURR?"
        assert SCPICommand.OVP_QUERY == "VOLT:LIM?"
        assert SCPICommand.OCP_QUERY == "CURR:LIM?"
        assert SCPICommand.MEASURE_ALL_INFO == "MEASure:ALL:INFO?"
        assert SCPICommand.MEASURE_ALL_MA == "MEASure:ALL?"
        assert SCPICommand.SYSTEM_REMOTE == "SYSTem:REMote"
        assert SCPICommand.SYSTEM_LOCAL == "SYSTem:LOCal"

    def test_builders(self):
        assert self.handler.build_identify() == "*IDN?"
        assert self.handler.build_output_on() == "OUTP ON"
        assert self.handler.build_output_off() == "OUTP OFF"
        assert self.handler.build_output_query() == "OUTP?"
        assert self.handler.build_set_voltage(12.5) == "VOLT 12.500"
        assert self.handler.build_query_voltage() == "VOLT?"
        assert self.handler.build_set_current(2.5) == "CURR 2.500"
        assert self.handler.build_query_current() == "CURR?"
        assert self.handler.build_set_ovp(15.0) == "VOLT:LIM 15.000"
        assert self.handler.build_query_ovp() == "VOLT:LIM?"
        assert self.handler.build_set_ocp(10.1) == "CURR:LIM 10.100"
        assert self.handler.build_query_ocp() == "CURR:LIM?"
        assert self.handler.build_measure_all(fit_ma=False) == "MEASure:ALL:INFO?"
        assert self.handler.build_measure_all(fit_ma=True) == "MEASure:ALL?"
        assert self.handler.build_system_remote() == "SYSTem:REMote"
        assert self.handler.build_system_local() == "SYSTem:LOCal"


class TestProtocolHandlerParsers:
    """Verify response decoding and malformed-payload rejection."""

    def setup_method(self):
        self.handler = ProtocolHandler()

    def test_parse_identity_full(self):
        raw = "OWON,SPE6103,2100040,FV:V1.0.0"
        ident = self.handler.parse_identity(raw)
        assert isinstance(ident, DeviceIdentity)
        assert ident.manufacturer == "OWON"
        assert ident.model == "SPE6103"
        assert ident.serial_number == "2100040"
        assert ident.firmware_version == "FV:V1.0.0"
        assert ident.raw_response == raw

    def test_parse_identity_three_fields(self):
        ident = self.handler.parse_identity("OWON,SPE6103,SN99")
        assert ident.serial_number == "SN99"
        assert ident.firmware_version == "unknown"

    def test_parse_identity_invalid_raises(self):
        with pytest.raises(ResponseError):
            self.handler.parse_identity("")
        with pytest.raises(ResponseError):
            self.handler.parse_identity("GARBAGE")

    def test_parse_float_variants(self):
        assert self.handler.parse_float("12") == 12.0
        assert self.handler.parse_float(" 12.500 ") == 12.5
        assert self.handler.parse_float("0.000") == 0.0
        assert self.handler.parse_float("-0.01") == -0.01

    def test_parse_float_invalid_raises(self):
        for bad in ("", "   ", "abc", "12.5V", "1,2"):
            with pytest.raises(ResponseError):
                self.handler.parse_float(bad)

    def test_parse_output_state(self):
        assert self.handler.parse_output_state("ON") == OutputState.ON
        assert self.handler.parse_output_state("off") == OutputState.OFF
        assert self.handler.parse_output_state("1") == OutputState.ON
        assert self.handler.parse_output_state("0") == OutputState.OFF

    def test_parse_output_state_invalid_raises(self):
        for bad in ("", "xyz", "2"):
            with pytest.raises(ResponseError):
                self.handler.parse_output_state(bad)

    @pytest.mark.parametrize(
        "code,expected_mode",
        [
            ("0", OperatingMode.STANDBY),
            ("1", OperatingMode.CV),
            ("2", OperatingMode.CC),
            ("3", OperatingMode.FAULT),
        ],
    )
    def test_parse_measure_all_info_modes(self, code, expected_mode):
        raw = f"12.000,1.500,18.000,OFF,OFF,OFF,{code}"
        res = self.handler.parse_measure_all(raw)
        assert isinstance(res, MeasureAllResult)
        assert res.measured_voltage == 12.0
        assert res.measured_current == 1.5
        assert res.measured_power == 18.0
        assert res.ovp_tripped is False
        assert res.ocp_tripped is False
        assert res.otp_tripped is False
        assert res.operating_mode == expected_mode
        assert res.fault_active is (expected_mode == OperatingMode.FAULT)

    def test_parse_measure_all_info_protection_tripped(self):
        raw = "0.000,0.000,0.000,ON,OFF,ON,3"
        res = self.handler.parse_measure_all(raw)
        assert res.ovp_tripped is True
        assert res.ocp_tripped is False
        assert res.otp_tripped is True
        assert res.operating_mode == OperatingMode.FAULT
        assert res.fault_active is True
        assert res.fault_summary == "OVP, OTP"

    def test_parse_measure_all_fit_ma_six_fields(self):
        raw = "5.000,0.500,2.500,OFF,ON,OFF"
        res = self.handler.parse_measure_all(raw, fit_ma=True)
        assert res.measured_voltage == 5.0
        assert res.ocp_tripped is True
        assert res.operating_mode == OperatingMode.UNKNOWN
        assert res.fault_active is True

    def test_parse_measure_all_malformed_raises(self):
        for bad in (
            "",
            "12.0,1.0",
            "abc,1.0,12.0,OFF,OFF,OFF,1",
            "12.0,1.0,12.0,BAD,OFF,OFF,1",
            "12.0,1.0,12.0,OFF,OFF,OFF,9",
        ):
            with pytest.raises(ResponseError):
                self.handler.parse_measure_all(bad, fit_ma=False)


class TestDeviceStatus:
    """Verify DeviceStatus formatting and properties."""

    def test_status_fault_summary(self):
        status = DeviceStatus(
            output_state=OutputState.OFF,
            set_voltage=12.0,
            set_current=2.0,
            measured_voltage=0.0,
            measured_current=0.0,
            measured_power=0.0,
            ovp_threshold=15.0,
            ocp_threshold=2.5,
            operating_mode=OperatingMode.FAULT,
            ocp_tripped=True,
        )
        assert status.is_output_on is False
        assert status.fault_active is True
        assert status.fault_summary == "OCP"
        text = str(status)
        assert "Fault" in text
        assert "OCP" in text
