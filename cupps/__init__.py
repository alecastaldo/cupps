"""A CUPPS 01.04 application-side interface library.

Implements the application half of the Common Use Passenger Processing
Systems Technical Specification, IATA Recommended Practice 1797 /
A4A RP 30.201 / ACI RP 500A07, document issue 01.04.0004 (02JUN2021).

Typical use::

    from cupps import CuppsEnvironment, PlatformSession, InterfaceMode

    env = CuppsEnvironment.from_os()
    with PlatformSession(
        env.platform_node, env.platform_port,
        airline=env.airline, event_token="...",
    ) as platform:
        runtime = platform.environment
        printer = runtime.first_of_type("PR")
"""

from .bcbp import BoardingPass, Leg, parse as parse_bcbp
from .crypto import decrypt_track, encrypt_track
from .devices import (
    AeaPrinter,
    BarcodeRead,
    LogDevice,
    OcrRead,
    PrintDocument,
    PrintDocumentResult,
    Printer,
    Reader,
    TrackRead,
)
from .env import CuppsEnvironment
from .errors import (
    ConnectionClosed,
    CuppsError,
    DeviceLocked,
    IllogicalMessage,
    RequestFailed,
    RequestTimeout,
    SessionError,
    TokenInvalidated,
)
from .model import (
    Device,
    DeviceStatus,
    PlatformInfo,
    RuntimeEnvironment,
    SupportedStock,
    WorkstationInfo,
)
from .results import (
    CryptAlgorithm,
    InterfaceMode,
    LockMethod,
    LogScope,
    LogSeverity,
)
from .session import SUPPORTED_INTERFACE_LEVELS, DeviceSession, PlatformSession

#: Version of this implementation, in the CUPPS component form of section 5.1.
__version__ = "01.00.0001"

#: The CUPPS Technical Specification issue this implements.
CUPPS_TS_VERSION = "01.04.0004"

__all__ = [
    "AeaPrinter",
    "BarcodeRead",
    "BoardingPass",
    "CUPPS_TS_VERSION",
    "ConnectionClosed",
    "CryptAlgorithm",
    "CuppsEnvironment",
    "CuppsError",
    "Device",
    "DeviceLocked",
    "DeviceSession",
    "DeviceStatus",
    "IllogicalMessage",
    "InterfaceMode",
    "Leg",
    "LockMethod",
    "LogDevice",
    "LogScope",
    "LogSeverity",
    "OcrRead",
    "PlatformInfo",
    "PlatformSession",
    "PrintDocument",
    "PrintDocumentResult",
    "Printer",
    "Reader",
    "RequestFailed",
    "RequestTimeout",
    "RuntimeEnvironment",
    "SUPPORTED_INTERFACE_LEVELS",
    "SessionError",
    "SupportedStock",
    "TokenInvalidated",
    "TrackRead",
    "WorkstationInfo",
    "__version__",
    "decrypt_track",
    "encrypt_track",
    "parse_bcbp",
]
