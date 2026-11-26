"""
Continuous monitoring system for the OWON SPE6103 power supply.

Captures timestamped snapshots containing:
    - Set voltage and set current limit
    - Measured output voltage, current, and power (via `MEASure:ALL:INFO?`)
    - OVP and OCP thresholds
    - Output ON/OFF state and operating mode (Standby/CV/CC/Fault)
    - Protection trip indicators (OVP, OCP, OTP)

All serial transactions serialize through PowerSupply's lock and SerialTransport's
lock so monitoring never collides with concurrent user/API commands.
"""

import csv
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional

from python_controller.exceptions import CommunicationError, TimeoutError

logger = logging.getLogger(__name__)


@dataclass
class Measurement:
    """Single timestamped telemetry and configuration snapshot."""

    timestamp: datetime
    set_voltage: float
    set_current: float
    measured_voltage: float
    measured_current: float
    measured_power: float
    output_state: bool
    ovp_threshold: Optional[float] = None
    ocp_threshold: Optional[float] = None
    operating_mode: str = "Unknown"
    ovp_tripped: bool = False
    ocp_tripped: bool = False
    otp_tripped: bool = False
    fault_active: bool = False
    error_message: str = ""

    @property
    def timestamp_iso(self) -> str:
        return self.timestamp.isoformat(timespec="milliseconds")

    @property
    def timestamp_epoch(self) -> float:
        return self.timestamp.timestamp()

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp_iso,
            "set_voltage_V": self.set_voltage,
            "set_current_A": self.set_current,
            "measured_voltage_V": self.measured_voltage,
            "measured_current_A": self.measured_current,
            "measured_power_W": self.measured_power,
            "output_state": "ON" if self.output_state else "OFF",
            "operating_mode": self.operating_mode,
            "ovp_threshold_V": self.ovp_threshold if self.ovp_threshold is not None else "",
            "ocp_threshold_A": self.ocp_threshold if self.ocp_threshold is not None else "",
            "ovp_tripped": self.ovp_tripped,
            "ocp_tripped": self.ocp_tripped,
            "otp_tripped": self.otp_tripped,
            "fault_active": self.fault_active,
            "fault_details": self.error_message,
        }

    def to_csv_row(self) -> list:
        return list(self.to_dict().values())

    @staticmethod
    def csv_header() -> list:
        return [
            "timestamp",
            "set_voltage_V",
            "set_current_A",
            "measured_voltage_V",
            "measured_current_A",
            "measured_power_W",
            "output_state",
            "operating_mode",
            "ovp_threshold_V",
            "ocp_threshold_A",
            "ovp_tripped",
            "ocp_tripped",
            "otp_tripped",
            "fault_active",
            "fault_details",
        ]

    def __str__(self) -> str:
        state = "ON" if self.output_state else "OFF"
        fault_str = f"  FAULT=[{self.error_message}]" if self.fault_active else ""
        return (
            f"[{self.timestamp_iso}] "
            f"V={self.measured_voltage:7.3f}V  "
            f"I={self.measured_current:7.3f}A  "
            f"P={self.measured_power:7.3f}W  "
            f"Out={state} ({self.operating_mode})"
            f"{fault_str}"
        )


class Monitor:
    """Thread-safe periodic poller with CSV export and optional sample cap."""

    def __init__(
        self,
        power_supply,
        interval: float = 1.0,
        csv_path: Optional[str] = None,
        include_settings: bool = True,
        include_protection: bool = True,
        max_samples: Optional[int] = None,
        callback: Optional[Callable[[Measurement], None]] = None,
    ):
        self._psu = power_supply
        self._interval = max(0.1, interval)
        self._csv_path = csv_path
        self._include_settings = include_settings
        self._include_protection = include_protection
        self._max_samples = max_samples
        self._callback = callback

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._measurements: List[Measurement] = []
        self._lock = threading.Lock()
        self._sample_count = 0
        self._error_count = 0
        self._csv_writer = None
        self._csv_file = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    @property
    def sample_count(self) -> int:
        return self._sample_count

    @property
    def error_count(self) -> int:
        return self._error_count

    @property
    def measurements(self) -> List[Measurement]:
        with self._lock:
            return list(self._measurements)

    @property
    def latest(self) -> Optional[Measurement]:
        with self._lock:
            return self._measurements[-1] if self._measurements else None

    def start(self) -> None:
        if self.is_running:
            raise RuntimeError("Monitor is already running.")

        self._stop_event.clear()
        self._sample_count = 0
        self._error_count = 0

        if self._csv_path:
            self._open_csv()

        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="PSU-Monitor",
            daemon=True,
        )
        self._running = True
        self._thread.start()
        logger.info(
            "Monitor started (interval=%.2fs, csv=%s)",
            self._interval,
            self._csv_path or "none",
        )

    def stop(self) -> None:
        if not self._running:
            return

        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 2.0)
            if self._thread.is_alive():
                logger.warning("Monitor thread did not stop within timeout.")

        self._close_csv()
        self._running = False
        logger.info(
            "Monitor stopped (%d samples, %d errors).",
            self._sample_count,
            self._error_count,
        )

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            loop_start = time.monotonic()

            try:
                measurement = self._take_measurement()
                with self._lock:
                    self._measurements.append(measurement)
                    self._sample_count += 1

                if self._csv_writer is not None and self._csv_file is not None:
                    try:
                        self._csv_writer.writerow(measurement.to_csv_row())
                        self._csv_file.flush()
                    except OSError as exc:
                        logger.error("CSV write error: %s", exc)

                if self._callback is not None:
                    try:
                        self._callback(measurement)
                    except Exception as exc:
                        logger.error("Monitor callback error: %s", exc)

                if self._max_samples and self._sample_count >= self._max_samples:
                    logger.info("Maximum sample count reached (%d).", self._max_samples)
                    break

            except (CommunicationError, TimeoutError) as exc:
                self._error_count += 1
                logger.warning("Monitor communication error (#%d): %s", self._error_count, exc)
                if self._error_count > 10:
                    logger.error("Too many monitoring errors (>10). Stopping.")
                    break
            except Exception as exc:
                self._error_count += 1
                logger.error("Unexpected monitor error (#%d): %s", self._error_count, exc)

            elapsed = time.monotonic() - loop_start
            remaining = self._interval - elapsed
            if remaining > 0:
                self._stop_event.wait(timeout=remaining)

        self._running = False

    def _take_measurement(self) -> Measurement:
        """
        Capture one atomic snapshot using `MEASure:ALL:INFO?`, `OUTP?`, and
        configured setpoints / protection thresholds.
        """
        timestamp = datetime.now()
        meas = self._psu.measure_all()
        output_on = self._psu.is_output_on()

        set_v = 0.0
        set_i = 0.0
        if self._include_settings:
            set_v = self._psu.get_voltage()
            set_i = self._psu.get_current_limit()

        ovp = None
        ocp = None
        if self._include_protection:
            try:
                ovp = self._psu.get_ovp()
            except Exception as exc:
                logger.debug("Monitor could not read OVP threshold: %s", exc)
            try:
                ocp = self._psu.get_ocp()
            except Exception as exc:
                logger.debug("Monitor could not read OCP threshold: %s", exc)

        return Measurement(
            timestamp=timestamp,
            set_voltage=set_v,
            set_current=set_i,
            measured_voltage=meas.measured_voltage,
            measured_current=meas.measured_current,
            measured_power=meas.measured_power,
            output_state=output_on,
            ovp_threshold=ovp,
            ocp_threshold=ocp,
            operating_mode=meas.operating_mode.value,
            ovp_tripped=meas.ovp_tripped,
            ocp_tripped=meas.ocp_tripped,
            otp_tripped=meas.otp_tripped,
            fault_active=meas.fault_active,
            error_message=meas.fault_summary,
        )

    def _open_csv(self) -> None:
        try:
            assert self._csv_path is not None
            csv_dir = os.path.dirname(self._csv_path)
            if csv_dir:
                os.makedirs(csv_dir, exist_ok=True)

            file_exists = os.path.exists(self._csv_path)
            self._csv_file = open(self._csv_path, "a", newline="", encoding="utf-8")
            self._csv_writer = csv.writer(self._csv_file)

            if not file_exists or os.path.getsize(self._csv_path) == 0:
                self._csv_writer.writerow(Measurement.csv_header())
                self._csv_file.flush()

            logger.info("CSV logging initialized: %s", self._csv_path)
        except OSError as exc:
            logger.error("Failed to open CSV file %s: %s", self._csv_path, exc)
            self._csv_writer = None
            self._csv_file = None

    def _close_csv(self) -> None:
        if self._csv_file is not None:
            try:
                self._csv_file.close()
            except OSError:
                pass
            self._csv_file = None
            self._csv_writer = None

    def __del__(self):
        if self._running:
            self.stop()
