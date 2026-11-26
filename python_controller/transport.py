"""
Transport layer for USB serial communication with the OWON SPE6103.

Derived from `delegate.serial.ComAgent` and `Owon\\conf\\pref.json`:
- Virtual COM port over USB-UART bridge ("MyCommCH340" CH340/CH341)
- Cross-platform Windows (`COMx`) and Linux / Raspberry Pi OS (`/dev/ttyUSBx`, `/dev/ttyACMx`)
- 115200 baud, 8 data bits, 1 stop bit, no parity, no flow control
- LF ('\\n') command/response termination
- 100 ms inter-command spacing matching `ComAgent` WriteDelay/ReadDelay
- Exclusive port locking on POSIX (`fcntl.flock`) and serialized lock per session
"""

import errno
import logging
import os
import threading
import time
from typing import List, Optional, Tuple

import serial
import serial.tools.list_ports

from python_controller.config import (
    CH340_USB_VID_PID,
    CH340_VENDOR_ID,
    COMMAND_TERMINATOR,
    DEFAULT_BAUDRATE,
    DEFAULT_TIMEOUT,
    DEFAULT_WRITE_TIMEOUT,
    IDN_MANUFACTURER,
    INTER_COMMAND_DELAY,
    MAX_RESPONSE_LENGTH,
)
from python_controller.exceptions import (
    CommunicationError,
    ConnectionError,
    ResponseError,
    TimeoutError,
)

logger = logging.getLogger(__name__)


def _is_ch340_port(port_info) -> bool:
    """Return True when `port_info` matches a QinHeng CH34x USB-UART bridge."""
    vid = getattr(port_info, "vid", None)
    pid = getattr(port_info, "pid", None)
    if vid is not None and pid is not None and (vid, pid) in CH340_USB_VID_PID:
        return True
    if vid == CH340_VENDOR_ID:
        return True
    desc = (getattr(port_info, "description", None) or "").upper()
    return any(tag in desc for tag in ("CH340", "CH341", "CH343", "CH9102"))


def _is_usb_serial_candidate(port_info) -> bool:
    """
    Filter serial ports to likely USB bridges while excluding Pi / motherboard
    onboard UARTs (`/dev/ttyAMA*`, `/dev/ttyS*`) unless backed by USB sysfs metadata.
    """
    if getattr(port_info, "vid", None) is not None:
        return True

    subsystem = (getattr(port_info, "subsystem", None) or "").lower()
    if subsystem in ("usb", "usb-serial"):
        return True

    device = getattr(port_info, "device", None) or ""
    if device.startswith(("/dev/ttyUSB", "/dev/ttyACM", "/dev/ttyXRUSB")):
        return True

    desc = (getattr(port_info, "description", None) or "").upper()
    hwid = (getattr(port_info, "hwid", None) or "").upper()
    combined = f"{desc} {hwid}"
    return any(tok in combined for tok in ("USB", "ACM", "CH340", "CH341", "CH343"))


def _classify_open_error(port_name: str, exc: Exception) -> Tuple[str, str]:
    """Classify serial open failure and format OS-aware remediation advice."""
    err_no = getattr(exc, "errno", None)
    msg = str(exc)
    lower_msg = msg.lower()
    is_posix_device = os.name == "posix" or str(port_name).startswith("/dev/")

    if (
        isinstance(exc, PermissionError)
        or err_no in (errno.EACCES, errno.EPERM)
        or "permission denied" in lower_msg
    ):
        hint = (
            " On Raspberry Pi OS / Linux, add your user to the 'dialout' group "
            "('sudo usermod -aG dialout $USER', then log out and back in) "
            "or add a udev permission rule."
            if is_posix_device
            else ""
        )
        return "permission", f"Permission denied opening {port_name}: {exc}.{hint}"

    busy_errnos = {
        code
        for code in (
            getattr(errno, "EBUSY", None),
            getattr(errno, "EAGAIN", None),
            getattr(errno, "EWOULDBLOCK", None),
        )
        if code is not None
    }
    if (
        err_no in busy_errnos
        or "exclusively lock" in lower_msg
        or "resource temporarily unavailable" in lower_msg
        or "access is denied" in lower_msg
    ):
        return (
            "busy",
            f"Serial port {port_name} is busy or locked by another process "
            f"(close other controllers, minicom, screen, or ModemManager): {exc}",
        )

    return "other", f"Failed to open serial port {port_name}: {exc}"


class SerialTransport:
    """
    Thread-safe serial transport for the OWON SPE6103 power supply.
    """

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = DEFAULT_BAUDRATE,
        timeout: float = DEFAULT_TIMEOUT,
        write_timeout: float = DEFAULT_WRITE_TIMEOUT,
        inter_command_delay: float = INTER_COMMAND_DELAY,
    ):
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._write_timeout = write_timeout
        self._inter_command_delay = max(0.0, inter_command_delay)

        self._serial: Optional[serial.Serial] = None
        self._lock = threading.RLock()
        self._last_command_time: float = 0.0
        self._connected = False

    @property
    def port(self) -> Optional[str]:
        """Configured or auto-detected serial port path."""
        return self._port

    @property
    def is_connected(self) -> bool:
        """Whether the serial port is currently open."""
        return self._connected and self._serial is not None and self._serial.is_open

    def _open_serial(
        self, port_name: str, timeout: float, write_timeout: float
    ) -> serial.Serial:
        """Open serial port with verified 8N1 framing and exclusive POSIX locking."""
        return serial.Serial(
            port=port_name,
            baudrate=self._baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
            write_timeout=write_timeout,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
            exclusive=True,
        )

    def connect(self) -> str:
        """
        Open the serial port, auto-detecting the SPE6103 port if none was given.

        Raises:
            ConnectionError: If no matching device is found or the port cannot open.
        """
        with self._lock:
            if self.is_connected:
                logger.warning("Already connected to %s", self._port)
                return self._port  # type: ignore[return-value]

            target_port = self._port
            if target_port is None:
                detected = self._auto_detect_port()
                if detected is None:
                    example_port = "/dev/ttyUSB0" if os.name == "posix" else "COM3"
                    raise ConnectionError(
                        "No OWON power supply detected. Connect the device via USB "
                        f"or specify a port explicitly (e.g., --port {example_port})."
                    )
                target_port = detected
                self._port = detected
                logger.info("Auto-detected OWON device on %s", target_port)

            try:
                self._serial = self._open_serial(
                    target_port,
                    timeout=self._timeout,
                    write_timeout=self._write_timeout,
                )
                self._serial.reset_input_buffer()
                self._serial.reset_output_buffer()
                self._connected = True
                logger.info(
                    "Connected to %s (%d 8N1)", target_port, self._baudrate
                )
                return target_port
            except (serial.SerialException, OSError) as exc:
                self._serial = None
                self._connected = False
                _, err_msg = _classify_open_error(target_port, exc)
                raise ConnectionError(err_msg) from exc

    def disconnect(self) -> None:
        """Close the serial port safely (no-op if already closed)."""
        with self._lock:
            if self._serial is not None:
                try:
                    if self._serial.is_open:
                        self._serial.close()
                        logger.info("Disconnected from %s", self._port)
                except (serial.SerialException, OSError) as exc:
                    logger.warning("Error closing serial port %s: %s", self._port, exc)
                finally:
                    self._serial = None
                    self._connected = False

    def send_command(
        self, command: str, expect_response: bool = True
    ) -> Optional[str]:
        """
        Transmit one LF-terminated ASCII command and optionally read one response line.

        Thread-safe: serialized via `self._lock` with `inter_command_delay` pacing.
        """
        with self._lock:
            if not self.is_connected or self._serial is None:
                raise CommunicationError(
                    "Not connected to power supply. Call connect() first."
                )

            self._wait_for_delay()

            try:
                # Discard any leftover bytes from an earlier unsolicited/stale frame
                if getattr(self._serial, "in_waiting", 0):
                    try:
                        if isinstance(self._serial.in_waiting, int) and self._serial.in_waiting > 0:
                            self._serial.reset_input_buffer()
                    except (TypeError, serial.SerialException, OSError):
                        pass

                payload = command.rstrip("\r\n") + COMMAND_TERMINATOR
                logger.debug("TX -> %r", payload)
                self._serial.write(payload.encode("ascii"))
                self._serial.flush()
                self._last_command_time = time.monotonic()

                if not expect_response:
                    return None

                response = self._read_response()
                logger.debug("RX <- %r", response)
                return response

            except serial.SerialTimeoutException as exc:
                raise TimeoutError(
                    f"Write timeout sending command {command!r}: {exc}"
                ) from exc
            except (serial.SerialException, OSError) as exc:
                self._connected = False
                raise CommunicationError(
                    f"Serial communication error during {command!r}: {exc}"
                ) from exc

    def send_raw(self, data: bytes) -> None:
        """Send raw bytes to the open serial port."""
        with self._lock:
            if not self.is_connected or self._serial is None:
                raise CommunicationError("Not connected to power supply.")
            try:
                logger.debug("TX raw -> %s", data.hex(" "))
                self._serial.write(data)
                self._serial.flush()
                self._last_command_time = time.monotonic()
            except serial.SerialTimeoutException as exc:
                raise TimeoutError(f"Write timeout sending raw bytes: {exc}") from exc
            except (serial.SerialException, OSError) as exc:
                self._connected = False
                raise CommunicationError(f"Failed to send raw data: {exc}") from exc

    def read_raw(self, size: int = MAX_RESPONSE_LENGTH) -> bytes:
        """Read raw bytes from the open serial port."""
        with self._lock:
            if not self.is_connected or self._serial is None:
                raise CommunicationError("Not connected to power supply.")
            try:
                data = self._serial.read(size)
                logger.debug("RX raw <- %s", data.hex(" ") if data else "(empty)")
                return data
            except (serial.SerialException, OSError) as exc:
                self._connected = False
                raise CommunicationError(f"Failed to read raw data: {exc}") from exc

    def _read_response(self) -> str:
        """Read one LF-terminated ASCII line within timeout."""
        assert self._serial is not None
        try:
            raw = self._serial.readline(MAX_RESPONSE_LENGTH)
        except serial.SerialTimeoutException as exc:
            raise TimeoutError(f"Read timeout waiting for response: {exc}") from exc

        if not raw:
            raise TimeoutError("No response received from device (read timeout).")

        decoded = raw.decode("ascii", errors="replace").strip()
        if not decoded:
            raise ResponseError(
                "Received blank line / whitespace-only response from device.",
                raw_response=repr(raw),
            )
        return decoded

    def _wait_for_delay(self) -> None:
        if self._last_command_time > 0 and self._inter_command_delay > 0:
            elapsed = time.monotonic() - self._last_command_time
            remaining = self._inter_command_delay - elapsed
            if remaining > 0:
                time.sleep(remaining)

    def _auto_detect_port(self) -> Optional[str]:
        """
        Enumerate serial ports, prioritizing CH340/CH34x USB-serial devices first,
        then any other USB serial port (excluding Pi onboard `/dev/ttyAMA*`),
        probing `*IDN?` for manufacturer "OWON".
        """
        logger.info("Auto-detecting OWON SPE6103 serial port...")
        prioritized: List[str] = []
        others: List[str] = []

        for port_info in serial.tools.list_ports.comports():
            logger.debug(
                "Candidate port %s (VID=%s, PID=%s, desc=%s, hwid=%s)",
                port_info.device,
                hex(port_info.vid) if port_info.vid is not None else "N/A",
                hex(port_info.pid) if port_info.pid is not None else "N/A",
                port_info.description,
                port_info.hwid,
            )
            if _is_ch340_port(port_info):
                prioritized.append(port_info.device)
            elif _is_usb_serial_candidate(port_info):
                others.append(port_info.device)

        blocked_reasons: List[str] = []
        for port_name in prioritized + others:
            try:
                logger.debug("Probing %s with *IDN?...", port_name)
                with self._open_serial(port_name, timeout=1.0, write_timeout=1.0) as probe:
                    probe.reset_input_buffer()
                    probe.reset_output_buffer()
                    probe.write(b"*IDN?\n")
                    probe.flush()
                    time.sleep(0.15)
                    resp = probe.readline(MAX_RESPONSE_LENGTH).decode(
                        "ascii", errors="replace"
                    ).strip()
                if resp and IDN_MANUFACTURER in resp.upper():
                    logger.info("Found OWON device on %s: %s", port_name, resp)
                    return port_name
            except (serial.SerialException, OSError) as exc:
                kind, detail = _classify_open_error(port_name, exc)
                logger.debug("Probe on %s failed (%s): %s", port_name, kind, detail)
                if kind in ("permission", "busy"):
                    blocked_reasons.append(detail)

        if blocked_reasons:
            raise ConnectionError(
                "Detected candidate USB serial port(s), but could not open them:\n  - "
                + "\n  - ".join(blocked_reasons)
            )

        return None

    @staticmethod
    def list_ports() -> List[Tuple[str, str, str]]:
        """List available serial ports as `(device, description, hwid)`."""
        return [
            (p.device, p.description, p.hwid)
            for p in serial.tools.list_ports.comports()
        ]

    def __enter__(self) -> "SerialTransport":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.disconnect()
        return False

    def __repr__(self) -> str:
        state = "connected" if self.is_connected else "disconnected"
        return f"SerialTransport(port={self._port!r}, baudrate={self._baudrate}, status={state})"
