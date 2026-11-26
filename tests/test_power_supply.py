"""
Unit tests for the high-level PowerSupply interface (`python_controller.power_supply`).
"""

from unittest.mock import MagicMock, patch

import pytest

from python_controller.exceptions import CommunicationError, ResponseError, SafetyError
from python_controller.power_supply import PowerSupply
from python_controller.protocol import OperatingMode, OutputState


@pytest.fixture
def mock_transport():
    with patch("python_controller.power_supply.SerialTransport") as MockTransport:
        mock = MockTransport.return_value
        mock.is_connected = True
        mock.port = "COM3"

        responses = {
            "*IDN?": "OWON,SPE6103,2100040,FV:V1.0.0",
            "VOLT?": "12.500",
            "CURR?": "2.000",
            "VOLT:LIM?": "61.000",
            "CURR:LIM?": "10.100",
            "OUTP?": "OFF",
            "MEASure:ALL:INFO?": "12.450,1.500,18.675,OFF,OFF,OFF,1",
            "MEASure:ALL?": "12.450,1.500,18.675,OFF,OFF,OFF",
        }

        def send_side_effect(cmd, expect_response=True):
            if cmd.startswith("VOLT "):
                responses["VOLT?"] = cmd.split()[1]
                return None
            if cmd.startswith("CURR "):
                responses["CURR?"] = cmd.split()[1]
                return None
            if cmd.startswith("VOLT:LIM "):
                responses["VOLT:LIM?"] = cmd.split()[1]
                return None
            if cmd.startswith("CURR:LIM "):
                responses["CURR:LIM?"] = cmd.split()[1]
                return None
            if cmd == "OUTP ON":
                responses["OUTP?"] = "ON"
                return None
            if cmd == "OUTP OFF":
                responses["OUTP?"] = "OFF"
                return None
            if not expect_response:
                return None
            return responses.get(cmd, "0")

        mock.send_command.side_effect = send_side_effect
        mock.connect.return_value = "COM3"
        yield mock, responses


class TestConnectionLifecycle:
    def test_connect_enters_remote_and_never_enables_output(self, mock_transport):
        mock, _ = mock_transport
        psu = PowerSupply(port="COM3")
        assert psu.connect() == "COM3"
        assert psu.is_connected
        cmds = [c.args[0] for c in mock.send_command.call_args_list]
        assert "*IDN?" in cmds
        assert "SYSTem:REMote" in cmds
        assert "OUTP ON" not in cmds

    def test_disconnect_safe_shutdown_turns_off_output_and_sets_local(self, mock_transport):
        mock, _ = mock_transport
        psu = PowerSupply(port="COM3")
        psu.connect()
        psu.disconnect(safe_shutdown=True)
        cmds = [c.args[0] for c in mock.send_command.call_args_list]
        assert "OUTP OFF" in cmds
        assert "SYSTem:LOCal" in cmds
        mock.disconnect.assert_called_once()


class TestSetpointsAndPowerBudget:
    def test_set_voltage_and_current_exact_commands(self, mock_transport):
        mock, _ = mock_transport
        psu = PowerSupply(port="COM3")
        psu.connect()
        psu.set_voltage(12.0)
        psu.set_current_limit(2.0)
        mock.send_command.assert_any_call("VOLT 12.000", expect_response=False)
        mock.send_command.assert_any_call("CURR 2.000", expect_response=False)

    def test_individual_limits_reject_out_of_range(self, mock_transport):
        psu = PowerSupply(port="COM3")
        psu.connect()
        with pytest.raises(SafetyError):
            psu.set_voltage(60.5)
        with pytest.raises(SafetyError):
            psu.set_voltage(-0.1)
        with pytest.raises(SafetyError):
            psu.set_current_limit(10.2)
        with pytest.raises(SafetyError):
            psu.set_current_limit(-0.1)

    def test_300w_power_budget_enforced_on_set_voltage(self, mock_transport):
        _, responses = mock_transport
        responses["CURR?"] = "10.000"
        psu = PowerSupply(port="COM3")
        psu.connect()
        psu.set_voltage(30.0)  # 30 * 10 = 300 W -> OK
        with pytest.raises(SafetyError, match="300 W"):
            psu.set_voltage(35.0)  # 35 * 10 = 350 W -> SafetyError

    def test_300w_power_budget_enforced_on_set_current(self, mock_transport):
        _, responses = mock_transport
        responses["VOLT?"] = "60.000"
        psu = PowerSupply(port="COM3")
        psu.connect()
        psu.set_current_limit(5.0)  # 60 * 5 = 300 W -> OK
        with pytest.raises(SafetyError, match="300 W"):
            psu.set_current_limit(5.5)  # 60 * 5.5 = 330 W -> SafetyError

    def test_configure_orders_writes_safely(self, mock_transport):
        mock, responses = mock_transport
        responses["VOLT?"] = "12.000"
        responses["CURR?"] = "10.000"
        psu = PowerSupply(port="COM3")
        psu.connect()
        mock.send_command.reset_mock()

        # Transitioning 12V/10A -> 60V/5A must lower current first (60*10=600W > 300W!)
        psu.configure(60.0, 5.0)
        sent = [c.args[0] for c in mock.send_command.call_args_list]
        assert sent == ["VOLT?", "CURR?", "CURR 5.000", "VOLT 60.000"]

        with pytest.raises(SafetyError, match="300 W"):
            psu.configure(60.0, 10.0)

    def test_output_on_refuses_excess_power_or_comm_failure(self, mock_transport):
        mock, responses = mock_transport
        responses["VOLT?"] = "60.000"
        responses["CURR?"] = "10.000"
        psu = PowerSupply(port="COM3")
        psu.connect()
        with pytest.raises(SafetyError):
            psu.output_on()

        # Also must not swallow comm failure before energizing
        mock.send_command.side_effect = CommunicationError("Serial dropped")
        with pytest.raises(CommunicationError):
            psu.output_on()


class TestProtectionOVPAndOCP:
    def test_ovp_and_ocp_use_lim_commands(self, mock_transport):
        mock, _ = mock_transport
        psu = PowerSupply(port="COM3")
        psu.connect()
        psu.set_ovp(15.0)
        psu.set_ocp(2.5)
        mock.send_command.assert_any_call("VOLT:LIM 15.000", expect_response=False)
        mock.send_command.assert_any_call("CURR:LIM 2.500", expect_response=False)
        assert psu.get_ovp() == 15.0
        assert psu.get_ocp() == 2.5

    def test_ovp_and_ocp_ceilings_match_winapp_config(self, mock_transport):
        psu = PowerSupply(port="COM3")
        psu.connect()
        psu.set_ovp(61.0)
        psu.set_ocp(10.1)
        with pytest.raises(SafetyError):
            psu.set_ovp(61.1)
        with pytest.raises(SafetyError):
            psu.set_ocp(10.2)


class TestMeasurementsAndStatus:
    def test_measure_methods_use_measure_all_info(self, mock_transport):
        mock, _ = mock_transport
        psu = PowerSupply(port="COM3")
        psu.connect()
        assert psu.measure_voltage() == 12.45
        assert psu.measure_current() == 1.5
        assert psu.measure_power() == 18.675
        mock.send_command.assert_any_call("MEASure:ALL:INFO?", expect_response=True)

    def test_get_status_captures_faults_and_mode(self, mock_transport):
        _, responses = mock_transport
        responses["OUTP?"] = "ON"
        responses["MEASure:ALL:INFO?"] = "0.000,0.000,0.000,OFF,ON,OFF,3"
        psu = PowerSupply(port="COM3")
        psu.connect()
        status = psu.get_status()
        assert status.output_state == OutputState.ON
        assert status.operating_mode == OperatingMode.FAULT
        assert status.ovp_tripped is False
        assert status.ocp_tripped is True
        assert status.otp_tripped is False
        assert status.fault_active is True
        assert status.fault_summary == "OCP"
