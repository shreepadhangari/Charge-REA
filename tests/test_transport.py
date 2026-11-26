"""
Unit tests for the serial transport layer (`python_controller.transport`).
"""

import threading
from unittest.mock import MagicMock, patch

import pytest
import serial

from python_controller.exceptions import (
    CommunicationError,
    ConnectionError,
    ResponseError,
    TimeoutError,
)
from python_controller.transport import SerialTransport


class TestSerialTransportInit:
    def test_defaults(self):
        t = SerialTransport()
        assert t.port is None
        assert not t.is_connected
        assert t._baudrate == 115200
        assert t._inter_command_delay == 0.1

    def test_custom_settings(self):
        t = SerialTransport(port="COM3", baudrate=9600, inter_command_delay=0.0)
        assert t.port == "COM3"
        assert t._baudrate == 9600
        assert "COM3" in repr(t)


class TestSerialTransportConnect:
    @patch("python_controller.transport.serial.Serial")
    def test_connect_success(self, mock_serial_cls):
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_serial_cls.return_value = mock_ser

        t = SerialTransport(port="COM3")
        assert t.connect() == "COM3"
        assert t.is_connected
        mock_ser.reset_input_buffer.assert_called_once()
        mock_ser.reset_output_buffer.assert_called_once()

    @patch("python_controller.transport.serial.Serial")
    def test_connect_failure_raises(self, mock_serial_cls):
        mock_serial_cls.side_effect = serial.SerialException("Port busy")
        t = SerialTransport(port="COM99")
        with pytest.raises(ConnectionError):
            t.connect()
        assert not t.is_connected

    @patch("python_controller.transport.serial.Serial")
    @patch("python_controller.transport.serial.tools.list_ports.comports")
    def test_auto_detect_prioritizes_ch340(self, mock_comports, mock_serial_cls):
        p_other = MagicMock()
        p_other.device = "COM2"
        p_other.vid = 0x0403
        p_other.pid = 0x6001
        p_other.description = "USB Serial"
        p_other.hwid = "USB VID:PID=0403:6001"

        p_ch340 = MagicMock()
        p_ch340.device = "COM5"
        p_ch340.vid = 0x1A86
        p_ch340.pid = 0x7523
        p_ch340.description = "USB-SERIAL CH340"
        p_ch340.hwid = "USB VID:PID=1A86:7523"

        mock_comports.return_value = [p_other, p_ch340]

        probe_ser = MagicMock()
        probe_ser.__enter__ = MagicMock(return_value=probe_ser)
        probe_ser.__exit__ = MagicMock(return_value=False)
        probe_ser.readline.return_value = b"OWON,SPE6103,2100040,FV:V1.0.0\n"

        main_ser = MagicMock()
        main_ser.is_open = True
        mock_serial_cls.side_effect = [probe_ser, main_ser]

        t = SerialTransport(inter_command_delay=0.0)
        assert t.connect() == "COM5"
        # First open call must target CH340 COM5 before COM2
        assert mock_serial_cls.call_args_list[0].kwargs["port"] == "COM5"

    def test_connect_auto_detect_none(self):
        t = SerialTransport()
        with patch.object(t, "_auto_detect_port", return_value=None):
            with pytest.raises(ConnectionError, match="No OWON"):
                t.connect()


class TestSerialTransportIO:
    @patch("python_controller.transport.serial.Serial")
    def test_send_and_receive(self, mock_serial_cls):
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_ser.in_waiting = 0
        mock_ser.readline.return_value = b"OWON,SPE6103,SN1,V1.0\n"
        mock_serial_cls.return_value = mock_ser

        t = SerialTransport(port="COM3", inter_command_delay=0.0)
        t.connect()
        resp = t.send_command("*IDN?")
        assert resp == "OWON,SPE6103,SN1,V1.0"
        mock_ser.write.assert_called_with(b"*IDN?\n")

    @patch("python_controller.transport.serial.Serial")
    def test_send_command_no_response(self, mock_serial_cls):
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_ser.in_waiting = 0
        mock_serial_cls.return_value = mock_ser

        t = SerialTransport(port="COM3", inter_command_delay=0.0)
        t.connect()
        assert t.send_command("OUTP ON", expect_response=False) is None
        mock_ser.write.assert_called_with(b"OUTP ON\n")

    @patch("python_controller.transport.serial.Serial")
    def test_read_timeout_raises(self, mock_serial_cls):
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_ser.in_waiting = 0
        mock_ser.readline.return_value = b""
        mock_serial_cls.return_value = mock_ser

        t = SerialTransport(port="COM3", inter_command_delay=0.0)
        t.connect()
        with pytest.raises(TimeoutError):
            t.send_command("*IDN?")

    @patch("python_controller.transport.serial.Serial")
    def test_blank_response_raises_response_error(self, mock_serial_cls):
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_ser.in_waiting = 0
        mock_ser.readline.return_value = b"   \n"
        mock_serial_cls.return_value = mock_ser

        t = SerialTransport(port="COM3", inter_command_delay=0.0)
        t.connect()
        with pytest.raises(ResponseError):
            t.send_command("VOLT?")

    def test_not_connected_raises(self):
        t = SerialTransport(port="COM3")
        with pytest.raises(CommunicationError, match="Not connected"):
            t.send_command("*IDN?")

    @patch("python_controller.transport.serial.Serial")
    def test_thread_safe_serialization(self, mock_serial_cls):
        active = 0
        overlap = False
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_ser.in_waiting = 0

        def fake_readline(_size=1024):
            nonlocal active, overlap
            active += 1
            if active > 1:
                overlap = True
            active -= 1
            return b"12.000\n"

        mock_ser.readline.side_effect = fake_readline
        mock_serial_cls.return_value = mock_ser

        t = SerialTransport(port="COM3", inter_command_delay=0.0)
        t.connect()

        threads = [
            threading.Thread(target=lambda: [t.send_command("VOLT?") for _ in range(15)])
            for _ in range(4)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert overlap is False

    @patch("python_controller.transport.serial.tools.list_ports.comports")
    def test_list_ports(self, mock_comports):
        p = MagicMock()
        p.device = "COM3"
        p.description = "USB-SERIAL CH340"
        p.hwid = "USB VID:PID=1A86:7523"
        mock_comports.return_value = [p]
        ports = SerialTransport.list_ports()
        assert ports == [("COM3", "USB-SERIAL CH340", "USB VID:PID=1A86:7523")]


class TestRaspberryPiLinuxCompatibility:
    @patch("python_controller.transport.serial.Serial")
    def test_connect_uses_exclusive_no_flow_control(self, mock_serial_cls):
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_serial_cls.return_value = mock_ser

        t = SerialTransport(port="/dev/ttyUSB0")
        assert t.connect() == "/dev/ttyUSB0"
        kwargs = mock_serial_cls.call_args.kwargs
        assert kwargs["exclusive"] is True
        assert kwargs["rtscts"] is False
        assert kwargs["dsrdtr"] is False
        assert kwargs["xonxoff"] is False

    @patch("python_controller.transport.serial.Serial")
    @patch("python_controller.transport.serial.tools.list_ports.comports")
    def test_auto_detect_pi5_skips_ttyama_and_detects_usb_and_acm(
        self, mock_comports, mock_serial_cls
    ):
        p_ama = MagicMock(
            device="/dev/ttyAMA10", vid=None, pid=None,
            subsystem="amba", description="ttyAMA10", hwid="107d001000.serial",
        )
        p_s0 = MagicMock(
            device="/dev/ttyS0", vid=None, pid=None,
            subsystem="pnp", description="ttyS0", hwid="PNP0501",
        )
        p_acm = MagicMock(
            device="/dev/ttyACM0", vid=None, pid=None,
            subsystem="usb", description="ttyACM0", hwid="USB-ACM",
        )
        p_ch340 = MagicMock(
            device="/dev/ttyUSB0", vid=0x1A86, pid=0x55D4,
            subsystem="usb-serial", description="USB Single Serial", hwid="USB VID:PID=1A86:55D4",
        )
        mock_comports.return_value = [p_ama, p_s0, p_acm, p_ch340]

        probe_ch340 = MagicMock()
        probe_ch340.__enter__ = MagicMock(return_value=probe_ch340)
        probe_ch340.__exit__ = MagicMock(return_value=False)
        probe_ch340.readline.return_value = b""  # First device isn't OWON

        probe_acm = MagicMock()
        probe_acm.__enter__ = MagicMock(return_value=probe_acm)
        probe_acm.__exit__ = MagicMock(return_value=False)
        probe_acm.readline.return_value = b"OWON,SPE6103,SN2,FV:V1.0\n"

        main_ser = MagicMock()
        main_ser.is_open = True
        mock_serial_cls.side_effect = [probe_ch340, probe_acm, main_ser]

        t = SerialTransport(inter_command_delay=0.0)
        assert t.connect() == "/dev/ttyACM0"
        probed_ports = [c.kwargs["port"] for c in mock_serial_cls.call_args_list]
        assert "/dev/ttyAMA10" not in probed_ports
        assert "/dev/ttyS0" not in probed_ports
        assert probed_ports == ["/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyACM0"]

    @patch("python_controller.transport.serial.Serial")
    def test_linux_dialout_permission_error_message(self, mock_serial_cls):
        mock_serial_cls.side_effect = serial.SerialException(
            13, "could not open port /dev/ttyUSB0: [Errno 13] Permission denied: '/dev/ttyUSB0'"
        )
        t = SerialTransport(port="/dev/ttyUSB0")
        with pytest.raises(ConnectionError, match="dialout"):
            t.connect()

    @patch("python_controller.transport.serial.Serial")
    @patch("python_controller.transport.serial.tools.list_ports.comports")
    def test_auto_detect_surfaces_dialout_permission_error(
        self, mock_comports, mock_serial_cls
    ):
        p_usb = MagicMock(
            device="/dev/ttyUSB0", vid=0x1A86, pid=0x7523,
            subsystem="usb-serial", description="USB-SERIAL CH340", hwid="USB VID:PID=1A86:7523",
        )
        mock_comports.return_value = [p_usb]
        mock_serial_cls.side_effect = serial.SerialException(
            13, "could not open port /dev/ttyUSB0: [Errno 13] Permission denied"
        )
        t = SerialTransport()
        with pytest.raises(ConnectionError, match="dialout"):
            t.connect()
