"""
Custom exception classes for the OWON SPE6103 power supply controller.

Exception Hierarchy:
    OwonError
    ├── CommunicationError
    │   ├── ConnectionError
    │   └── TimeoutError
    ├── CommandError
    │   └── ResponseError
    ├── SafetyError
    └── DeviceError
"""


class OwonError(Exception):
    """Base exception for all OWON power supply errors."""


class CommunicationError(OwonError):
    """Error in serial/USB communication with the power supply."""


class ConnectionError(CommunicationError):
    """Failed to establish or maintain connection to the power supply."""


class TimeoutError(CommunicationError):
    """Communication timed out waiting for device response."""


class CommandError(OwonError):
    """Error sending a command or the command was rejected/unsupported."""


class ResponseError(CommandError):
    """
    Response from the device was invalid, malformed, or unexpected.

    Attributes:
        raw_response: The raw response string received from the device.
    """

    def __init__(self, message: str, raw_response: str = ""):
        super().__init__(message)
        self.raw_response = raw_response


class SafetyError(OwonError):
    """
    A requested operation would violate safety or hardware operating limits.

    Attributes:
        parameter: Parameter that exceeded limits ('voltage', 'current', 'power', 'ovp', 'ocp').
        requested_value: Requested numeric value.
        limit_value: Maximum or allowed limit value.
    """

    def __init__(
        self,
        message: str,
        parameter: str = "",
        requested_value: float = 0.0,
        limit_value: float = 0.0,
    ):
        super().__init__(message)
        self.parameter = parameter
        self.requested_value = requested_value
        self.limit_value = limit_value


class DeviceError(OwonError):
    """
    Hardware protection fault or device error state reported by the supply
    (e.g., OVP, OCP, OTP, or Fault mode reported via MEASure:ALL:INFO?).

    Attributes:
        error_code: Numeric code or 0 if reported via status flag.
        error_message: Description of the fault or error.
    """

    def __init__(self, message: str, error_code: int = 0, error_message: str = ""):
        super().__init__(message)
        self.error_code = error_code
        self.error_message = error_message
