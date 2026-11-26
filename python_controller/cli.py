"""
Command-line interface for the OWON SPE6103 programmable DC power supply.

Examples:
    python main.py --list-ports
    python main.py --identify
    python main.py --voltage 12 --current 2
    python main.py --ovp 15 --ocp 2.5
    python main.py --output on
    python main.py --output off
    python main.py --status
    python main.py --monitor
    python main.py --monitor --interval 1 --csv measurements.csv
"""

import argparse
import logging
import signal
import sys
import threading
import time
from typing import Optional

from python_controller.config import ConnectionConfig, SPE6103_LIMITS
from python_controller.exceptions import OwonError, SafetyError
from python_controller.monitor import Monitor
from python_controller.power_supply import PowerSupply
from python_controller.transport import SerialTransport


def setup_logging(verbose: bool = False, debug: bool = False) -> None:
    """Configure console logging."""
    if debug:
        level = logging.DEBUG
        fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    elif verbose:
        level = logging.INFO
        fmt = "%(asctime)s [%(levelname)s] %(message)s"
    else:
        level = logging.WARNING
        fmt = "%(message)s"

    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description=(
            "OWON SPE6103 Programmable DC Power Supply Controller\n"
            "Control, protect, and monitor the power supply over USB serial."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --list-ports\n"
            "  %(prog)s --identify\n"
            "  %(prog)s --voltage 12 --current 2\n"
            "  %(prog)s --ovp 15 --ocp 2.5\n"
            "  %(prog)s --output on\n"
            "  %(prog)s --output off\n"
            "  %(prog)s --status\n"
            "  %(prog)s --monitor --interval 1 --csv measurements.csv\n"
        ),
    )

    conn_group = parser.add_argument_group("Connection")
    conn_group.add_argument(
        "--port", "-p",
        type=str,
        default=None,
        help="Serial COM port (e.g., COM3, /dev/ttyUSB0). Auto-detected if omitted.",
    )
    conn_group.add_argument(
        "--baudrate", "-b",
        type=int,
        default=115200,
        help="Serial baud rate (default: 115200).",
    )
    conn_group.add_argument(
        "--timeout",
        type=float,
        default=2.0,
        help="Serial timeout in seconds (default: 2.0).",
    )
    conn_group.add_argument(
        "--fit-ma",
        action="store_true",
        help="Use legacy MEASure:ALL? query instead of MEASure:ALL:INFO?.",
    )

    info_group = parser.add_argument_group("Information")
    info_group.add_argument(
        "--identify", "-i",
        action="store_true",
        help="Query and display *IDN? device identification.",
    )
    info_group.add_argument(
        "--status", "-s",
        action="store_true",
        help="Display comprehensive status (setpoints, measurements, protection, faults).",
    )
    info_group.add_argument(
        "--measure", "-m",
        action="store_true",
        help="Take a single telemetry reading via MEASure:ALL:INFO?.",
    )
    info_group.add_argument(
        "--list-ports",
        action="store_true",
        help="List all available serial ports and exit.",
    )

    config_group = parser.add_argument_group("Configuration")
    config_group.add_argument(
        "--voltage", "-V",
        type=float,
        default=None,
        help=f"Set voltage setpoint in V (0..{SPE6103_LIMITS.max_voltage:.1f}, max power {SPE6103_LIMITS.max_power:.0f} W).",
    )
    config_group.add_argument(
        "--current", "-I",
        type=float,
        default=None,
        help=f"Set current limit in A (0..{SPE6103_LIMITS.max_current:.1f}, max power {SPE6103_LIMITS.max_power:.0f} W).",
    )
    config_group.add_argument(
        "--ovp",
        type=float,
        default=None,
        help=f"Set independent OVP threshold via VOLT:LIM (0..{SPE6103_LIMITS.max_ovp:.1f} V).",
    )
    config_group.add_argument(
        "--ocp",
        type=float,
        default=None,
        help=f"Set independent OCP threshold via CURR:LIM (0..{SPE6103_LIMITS.max_ocp:.1f} A).",
    )

    output_group = parser.add_argument_group("Output Control")
    output_group.add_argument(
        "--output", "-o",
        type=str,
        choices=["on", "off", "ON", "OFF"],
        default=None,
        help="Explicitly enable or disable DC output (OUTP ON / OUTP OFF).",
    )

    monitor_group = parser.add_argument_group("Monitoring")
    monitor_group.add_argument(
        "--monitor",
        action="store_true",
        help="Run continuous monitoring. Press Ctrl+C to stop (performs safe output-off shutdown).",
    )
    monitor_group.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Monitoring poll interval in seconds (default: 1.0, min: 0.1).",
    )
    monitor_group.add_argument(
        "--csv",
        type=str,
        default=None,
        help="Path to CSV file for logging timestamped measurements.",
    )
    monitor_group.add_argument(
        "--max-samples",
        type=int,
        default=None,
        help="Maximum monitoring samples before stopping (default: unlimited).",
    )

    sys_group = parser.add_argument_group("Raw Command")
    sys_group.add_argument(
        "--raw",
        type=str,
        default=None,
        help="Send an explicit ASCII/SCPI command string and print response if ending in '?'.",
    )

    parser.add_argument("--verbose", "-v", action="store_true", help="Enable INFO logging.")
    parser.add_argument("--debug", action="store_true", help="Enable DEBUG serial trace logging.")
    return parser


def validate_cli_inputs(args: argparse.Namespace) -> None:
    """Validate CLI inputs offline against hardware capabilities before opening COM port."""
    limits = SPE6103_LIMITS

    if args.voltage is not None and (args.voltage < limits.min_voltage or args.voltage > limits.max_voltage):
        raise SafetyError(
            f"Voltage {args.voltage:.3f} V is outside allowed range "
            f"[{limits.min_voltage:.1f}, {limits.max_voltage:.1f}] V.",
            parameter="voltage",
            requested_value=args.voltage,
            limit_value=limits.max_voltage,
        )

    if args.current is not None and (args.current < limits.min_current or args.current > limits.max_current):
        raise SafetyError(
            f"Current {args.current:.3f} A is outside allowed range "
            f"[{limits.min_current:.1f}, {limits.max_current:.1f}] A.",
            parameter="current",
            requested_value=args.current,
            limit_value=limits.max_current,
        )

    if args.voltage is not None and args.current is not None:
        power = args.voltage * args.current
        if power > limits.max_power + 1e-6:
            raise SafetyError(
                f"Requested power {power:.2f} W ({args.voltage:.3f} V x {args.current:.3f} A) "
                f"exceeds maximum rated power {limits.max_power:.0f} W.",
                parameter="power",
                requested_value=power,
                limit_value=limits.max_power,
            )

    if args.ovp is not None and (args.ovp < limits.min_ovp or args.ovp > limits.max_ovp):
        raise SafetyError(
            f"OVP threshold {args.ovp:.3f} V is outside allowed range "
            f"[{limits.min_ovp:.1f}, {limits.max_ovp:.1f}] V.",
            parameter="ovp",
            requested_value=args.ovp,
            limit_value=limits.max_ovp,
        )

    if args.ocp is not None and (args.ocp < limits.min_ocp or args.ocp > limits.max_ocp):
        raise SafetyError(
            f"OCP threshold {args.ocp:.3f} A is outside allowed range "
            f"[{limits.min_ocp:.1f}, {limits.max_ocp:.1f}] A.",
            parameter="ocp",
            requested_value=args.ocp,
            limit_value=limits.max_ocp,
        )


def cmd_list_ports() -> None:
    ports = SerialTransport.list_ports()
    if not ports:
        print("No serial ports found.")
        return

    print(f"\n{'Port':<12} {'Description':<40} {'Hardware ID'}")
    print("-" * 80)
    for port, desc, hwid in ports:
        print(f"{port:<12} {desc:<40} {hwid}")
    print()


def cmd_identify(psu: PowerSupply) -> None:
    identity = psu.identify()
    print("\nDevice Identification:")
    print(f"  Manufacturer:     {identity.manufacturer}")
    print(f"  Model:            {identity.model}")
    print(f"  Serial Number:    {identity.serial_number}")
    print(f"  Firmware Version: {identity.firmware_version}")
    print(f"  Raw Response:     {identity.raw_response}\n")


def cmd_status(psu: PowerSupply) -> None:
    status = psu.get_status()
    print("\nPower Supply Status:")
    print(f"  Output State:     {'ON' if status.is_output_on else 'OFF'}")
    print(f"  Operating Mode:   {status.operating_mode.value}")
    print(f"  Set Voltage:      {status.set_voltage:.3f} V")
    print(f"  Set Current:      {status.set_current:.3f} A")
    print(f"  Measured Voltage: {status.measured_voltage:.3f} V")
    print(f"  Measured Current: {status.measured_current:.3f} A")
    print(f"  Measured Power:   {status.measured_power:.3f} W")
    if status.ovp_threshold is not None:
        print(f"  OVP Threshold:    {status.ovp_threshold:.3f} V (tripped={status.ovp_tripped})")
    if status.ocp_threshold is not None:
        print(f"  OCP Threshold:    {status.ocp_threshold:.3f} A (tripped={status.ocp_tripped})")
    print(f"  OTP Tripped:      {status.otp_tripped}")
    if status.fault_active:
        print(f"  Active Faults:    {status.fault_summary}")
    print()


def cmd_measure(psu: PowerSupply) -> None:
    meas = psu.measure_all()
    output = psu.is_output_on()
    print("\nMeasurement:")
    print(f"  Voltage: {meas.measured_voltage:.3f} V")
    print(f"  Current: {meas.measured_current:.3f} A")
    print(f"  Power:   {meas.measured_power:.3f} W")
    print(f"  Output:  {'ON' if output else 'OFF'}")
    print(f"  Mode:    {meas.operating_mode.value}")
    print(
        f"  Trips:   OVP={meas.ovp_tripped}, OCP={meas.ocp_tripped}, OTP={meas.otp_tripped}\n"
    )


def cmd_monitor(
    psu: PowerSupply,
    interval: float,
    csv_path: Optional[str],
    max_samples: Optional[int],
) -> None:
    stop_requested = False

    def signal_handler(sig, frame):
        nonlocal stop_requested
        stop_requested = True
        print("\n\nStopping monitor (safe shutdown)...")

    is_main_thread = threading.current_thread() is threading.main_thread()
    prev_sigint = None
    prev_sigterm = None
    if is_main_thread:
        prev_sigint = signal.signal(signal.SIGINT, signal_handler)
        if hasattr(signal, "SIGTERM"):
            prev_sigterm = signal.signal(signal.SIGTERM, signal_handler)

    def print_measurement(m):
        print(m)

    monitor = Monitor(
        power_supply=psu,
        interval=interval,
        csv_path=csv_path,
        max_samples=max_samples,
        callback=print_measurement,
    )

    print(f"\nStarting continuous monitoring (interval={max(0.1, interval):.2f}s)")
    if csv_path:
        print(f"Logging CSV to: {csv_path}")
    print("Press Ctrl+C to stop.\n")

    monitor.start()
    try:
        while monitor.is_running and not stop_requested:
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()
        if is_main_thread:
            if prev_sigint is not None:
                signal.signal(signal.SIGINT, prev_sigint)
            if prev_sigterm is not None and hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, prev_sigterm)

    print(
        f"\nMonitoring complete: {monitor.sample_count} samples, "
        f"{monitor.error_count} errors."
    )


def main(argv=None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)
    setup_logging(verbose=args.verbose, debug=args.debug)

    if args.list_ports:
        cmd_list_ports()
        return 0

    has_action = any(
        [
            args.identify,
            args.status,
            args.measure,
            args.voltage is not None,
            args.current is not None,
            args.ovp is not None,
            args.ocp is not None,
            args.output is not None,
            args.monitor,
            args.raw is not None,
        ]
    )
    if not has_action:
        parser.print_help()
        return 0

    # Validate user parameters up front before connecting to hardware
    try:
        validate_cli_inputs(args)
    except SafetyError as exc:
        print(f"SAFETY ERROR: {exc}", file=sys.stderr)
        return 2

    config = ConnectionConfig(
        port=args.port,
        baudrate=args.baudrate,
        timeout=args.timeout,
        fit_ma_protocol=args.fit_ma,
    )
    psu = PowerSupply(port=args.port, config=config)

    try:
        psu.connect()
    except OwonError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    exit_code = 0
    # Leave output ON only when explicitly requested via `--output on` without monitor/error
    keep_output_enabled = False

    try:
        if args.identify:
            cmd_identify(psu)

        # Apply protection thresholds before output setpoints
        if args.ovp is not None:
            psu.set_ovp(args.ovp)
            print(f"OVP threshold (VOLT:LIM) set to {args.ovp:.3f} V")

        if args.ocp is not None:
            psu.set_ocp(args.ocp)
            print(f"OCP threshold (CURR:LIM) set to {args.ocp:.3f} A")

        # Apply voltage & current setpoints safely
        if args.voltage is not None and args.current is not None:
            psu.configure(args.voltage, args.current)
            print(f"Voltage set to {args.voltage:.3f} V, current limit to {args.current:.3f} A")
        elif args.voltage is not None:
            psu.set_voltage(args.voltage)
            print(f"Voltage set to {args.voltage:.3f} V")
        elif args.current is not None:
            psu.set_current_limit(args.current)
            print(f"Current limit set to {args.current:.3f} A")

        if args.output is not None:
            if args.output.lower() == "on":
                psu.output_on()
                print("Output ENABLED (warning: terminals live; '--output off' to disable).")
                if not args.monitor:
                    keep_output_enabled = True
            else:
                psu.output_off()
                keep_output_enabled = False
                print("Output DISABLED (note: verify terminal discharge before touching load).")

        if args.raw is not None:
            resp = psu.raw_command(args.raw)
            if resp is not None:
                print(f"Response: {resp}")

        if args.status:
            cmd_status(psu)

        if args.measure:
            cmd_measure(psu)

        if args.monitor:
            cmd_monitor(psu, args.interval, args.csv, args.max_samples)
            # Exiting a monitor session safely powers down if user enabled output for the run
            keep_output_enabled = False

    except SafetyError as exc:
        print(f"\nSAFETY ERROR: {exc}", file=sys.stderr)
        keep_output_enabled = False
        exit_code = 2
    except OwonError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        keep_output_enabled = False
        exit_code = 1
    except KeyboardInterrupt:
        print("\nInterrupted by user; performing safe shutdown.")
        keep_output_enabled = False
        exit_code = 130
    finally:
        # Read-only queries (--identify, --status, --measure) should NOT unexpectedly alter existing
        # output state unless we ran output control / monitoring or hit an error/interrupt; wait:
        # for read-only CLI checks, safe_shutdown=False leaves user's bench supply state untouched,
        # OR should non-`--output on` leave output off?
        # Look at Phase 4: `--identify`, `--status`, `--measure`, `--voltage 12 --current 2`, `--ovp 15 --ocp 2.5`.
        # If the user just checks `--status` or `--measure` while output is ON, forcing OUTP OFF would
        # surprise them! Meanwhile, if an error/Ctrl+C occurred or `--output off` or `--monitor` ended,
        # safe_shutdown=True guarantees output is OFF! Let's apply safe_shutdown cleanly:
        safe_shutdown = (
            exit_code != 0
            or (args.output is not None and args.output.lower() == "off")
            or args.monitor
        )
        if keep_output_enabled:
            safe_shutdown = False
        try:
            psu.disconnect(safe_shutdown=safe_shutdown)
        except Exception:
            pass

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
