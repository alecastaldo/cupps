"""Physical peripheral drivers for the CUPPS platform.

Three layers, deliberately separate:

``transport``
    Moves bytes. Serial, TCP, loopback, or a pseudo-terminal for testing.
``base`` and the concrete drivers
    Speak a device's protocol over a transport, and enforce the logical
    securing that section 10.4.1 requires of an unheld device.
``registry``
    Binds a logical CUPPS device name to a driver and a transport, from
    configuration rather than code.

The split is what lets the same AEA printer sit on serial at one station and
TCP at the next, and what lets a lab bench swap a real printer for a
pseudo-terminal without a code change.
"""

from .aea import AeaDriver
from .base import (
    DeviceDriver,
    DeviceSecured,
    DriverData,
    DriverError,
    DriverStatus,
)
from .printer import PrintBackend, PrintDriver, PrintError, PrintResult
from .reader import ReaderDriver
from .registry import (
    BindingError,
    BindingRegistry,
    DeviceBinding,
    register_driver,
)
from .transport import (
    LoopbackTransport,
    PtyTransport,
    SerialTransport,
    TcpTransport,
    Transport,
    TransportConfig,
    TransportError,
    build_transport,
    normalise_port,
)

__all__ = [
    "AeaDriver",
    "BindingError",
    "BindingRegistry",
    "DeviceBinding",
    "DeviceDriver",
    "DeviceSecured",
    "DriverData",
    "DriverError",
    "DriverStatus",
    "LoopbackTransport",
    "PrintBackend",
    "PrintDriver",
    "PrintError",
    "PrintResult",
    "PtyTransport",
    "ReaderDriver",
    "SerialTransport",
    "TcpTransport",
    "Transport",
    "TransportConfig",
    "TransportError",
    "build_transport",
    "normalise_port",
    "register_driver",
]
