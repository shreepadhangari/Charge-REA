"""
Unit tests for continuous monitoring (`Monitor`), CSV export, concurrency, and CLI (`main.py`).
"""

import csv
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from python_controller import cli
from python_controller.monitor import Measurement, Monitor
from python_controller.power_supply import PowerSupply


@pytest.fixture
def mock_psu_transport():
    with patch("python_controller.power_supply.SerialTransport") as MockTransport:
        mock = MockTransport.return_value
        mock.is_connected = True
        mock.port = "COM3"

        state = {
            "VOLT?": "12.000",
            "CURR?": "2.000",
            "VOLT:LIM?": "15.000",
            "CURR:LIM?": "2.500",
            "OUTP?": "OFF",
            "MEASure:ALL:INFO?": "11.980,1.250,14.975,OFF,OFF,OFF,1",
        }
        lock = threading.Lock()

        def side_effect(cmd, expect_response=True):
            with lock:
                if cmd == "*IDN?":
                    return "OWON,SPE6103,2100040,FV:V1.0.0"
                if cmd.startswith("VOLT "):
                    state["VOLT?"] = cmd.split()[1]
                    return None
                if cmd.startswith("CURR "):
                    state["CURR?"] = cmd.split()[1]
                    return None
                if cmd.startswith("VOLT:LIM "):
                    state["VOLT:LIM?"] = cmd.split()[1]
                    return None
                if cmd.startswith("CURR:LIM "):
                    state["CURR:LIM?"] = cmd.split()[1]
                    return None
                if cmd == "OUTP ON":
                    state["OUTP?"] = "ON"
                    return None
                if cmd == "OUTP OFF":
                    state["OUTP?"] = "OFF"
                    return None
                if not expect_response:
                    return None
                return state.get(cmd, "0")

        mock.send_command.side_effect = side_effect
        mock.connect.return_value = "COM3"
        yield mock, state


class TestMonitorAndCSV:
    def test_monitor_captures_all_fields_and_writes_csv(self, mock_psu_transport, tmp_path):
        _, state = mock_psu_transport
        state["OUTP?"] = "ON"
        state["MEASure:ALL:INFO?"] = "12.010,1.500,18.015,OFF,ON,OFF,2"

        csv_file = tmp_path / "measurements.csv"
        psu = PowerSupply(port="COM3")
        psu.connect()

        mon = Monitor(psu, interval=0.1, csv_path=str(csv_file), max_samples=3)
        mon.start()

        deadline = time.monotonic() + 3.0
        while mon.sample_count < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        mon.stop()

        assert mon.sample_count == 3
        assert mon.error_count == 0
        latest = mon.latest
        assert isinstance(latest, Measurement)
        assert latest.set_voltage == 12.0
        assert latest.set_current == 2.0
        assert latest.measured_voltage == 12.01
        assert latest.measured_current == 1.5
        assert latest.measured_power == 18.015
        assert latest.ovp_threshold == 15.0
        assert latest.ocp_threshold == 2.5
        assert latest.output_state is True
        assert latest.operating_mode == "Constant Current"
        assert latest.ocp_tripped is True
        assert latest.fault_active is True

        with open(csv_file, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        assert rows[0] == Measurement.csv_header()
        assert len(rows) == 4  # header + 3 samples

    def test_concurrent_commands_while_monitoring(self, mock_psu_transport):
        psu = PowerSupply(port="COM3")
        psu.connect()

        mon = Monitor(psu, interval=0.1)
        mon.start()
        try:
            for v in (5.0, 10.0, 12.0):
                psu.set_voltage(v)
                time.sleep(0.05)
        finally:
            mon.stop()

        assert mon.error_count == 0
        assert psu.get_voltage() == 12.0


class TestCLI:
    def test_cli_help_when_no_args(self, capsys):
        assert cli.main([]) == 0
        out = capsys.readouterr().out
        assert "OWON SPE6103" in out

    def test_cli_rejects_excess_power_offline(self, capsys):
        # 60 V * 10 A = 600 W > 300 W must fail cleanly with exit code 2 before connecting
        with patch("python_controller.cli.PowerSupply") as MockPSU:
            code = cli.main(["--voltage", "60", "--current", "10"])
            assert code == 2
            MockPSU.assert_not_called()
        assert "SAFETY ERROR" in capsys.readouterr().err

    def test_cli_passes_baudrate_and_timeout_to_config(self, mock_psu_transport):
        with patch("python_controller.cli.PowerSupply") as MockPSU:
            inst = MockPSU.return_value
            inst.identify.return_value = MagicMock(
                manufacturer="OWON", model="SPE6103", serial_number="1",
                firmware_version="V1", raw_response="OWON,SPE6103,1,V1",
            )
            code = cli.main(["--port", "COM4", "--baudrate", "9600", "--timeout", "4.0", "--identify"])
            assert code == 0
            cfg = MockPSU.call_args.kwargs["config"]
            assert cfg.port == "COM4"
            assert cfg.baudrate == 9600
            assert cfg.timeout == 4.0

    def test_cli_output_on_does_not_immediately_shut_off(self, mock_psu_transport):
        mock, _ = mock_psu_transport
        code = cli.main(["--port", "COM3", "--output", "on"])
        assert code == 0
        cmds = [c.args[0] for c in mock.send_command.call_args_list]
        assert "OUTP ON" in cmds
        assert "OUTP OFF" not in cmds
        assert "SYSTem:LOCal" in cmds

    def test_cli_output_off_sends_outp_off(self, mock_psu_transport):
        mock, _ = mock_psu_transport
        code = cli.main(["--port", "COM3", "--output", "off"])
        assert code == 0
        cmds = [c.args[0] for c in mock.send_command.call_args_list]
        assert "OUTP OFF" in cmds

    def test_cli_combined_settings_and_protection(self, mock_psu_transport):
        mock, _ = mock_psu_transport
        assert cli.main(["--port", "COM3", "--ovp", "15", "--ocp", "2.5"]) == 0
        mock.send_command.assert_any_call("VOLT:LIM 15.000", expect_response=False)
        mock.send_command.assert_any_call("CURR:LIM 2.500", expect_response=False)

        assert cli.main(["--port", "COM3", "--voltage", "12", "--current", "2"]) == 0
        mock.send_command.assert_any_call("VOLT 12.000", expect_response=False)
        mock.send_command.assert_any_call("CURR 2.000", expect_response=False)

    def test_cli_monitor_safe_from_worker_thread(self, mock_psu_transport, tmp_path):
        psu = PowerSupply(port="/dev/ttyUSB0")
        psu.connect()
        errors = []

        def run_worker():
            try:
                cli.cmd_monitor(psu, interval=0.05, csv_path=str(tmp_path / "w.csv"), max_samples=2)
            except Exception as exc:
                errors.append(exc)

        th = threading.Thread(target=run_worker)
        th.start()
        th.join(timeout=3.0)
        assert not th.is_alive()
        assert errors == []

    def test_cli_monitor_hooks_sigint_and_sigterm_when_available(self, mock_psu_transport):
        import signal

        psu = PowerSupply(port="/dev/ttyUSB0")
        psu.connect()
        with patch("python_controller.cli.signal.signal", wraps=signal.signal) as mock_sig:
            cli.cmd_monitor(psu, interval=0.05, csv_path=None, max_samples=1)
            hooked_signals = [c.args[0] for c in mock_sig.call_args_list]
            assert signal.SIGINT in hooked_signals
            if hasattr(signal, "SIGTERM"):
                assert signal.SIGTERM in hooked_signals
