"""Device drivers: the platform's side of a physical peripheral.

A driver binds a :class:`~cuppsplatform.drivers.transport.Transport` to one
logical CUPPS device and speaks that device's protocol.  It reports two
things upward -- data the device produced, and changes in the device's status
-- and accepts work downward.

Two rules shape the design.

**Securing (section 10.4.1).**  A platform must logically secure a device
when it is not in use: a barcode reader that no application holds "must be
secured such that it cannot be used (not even for using configuration
barcodes) or that any barcodes read are ignored".  The reason is specific --
a scanned configuration barcode can reprogram the reader and alter the
system.  So a driver starts secured, discards everything it reads while
secured, and refuses to write.  Unsecuring is the platform's decision, made
when an application acquires the device.

**Never block the platform.**  A driver's reader runs on its own thread and
hands data to callbacks that must not hold platform locks.  A peripheral that
stops responding is common; a peripheral that freezes the platform is an
outage.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional

from .transport import Transport, TransportError

log = logging.getLogger("cuppsplatform.drivers")


class DriverError(Exception):
    """The driver could not carry out an operation."""


class DeviceSecured(DriverError):
    """An operation was attempted on a device that is logically secured.

    Section 10.4.1: a device no application holds must not be operable.
    """


@dataclass(frozen=True)
class DriverStatus:
    """The device's condition, in the terms of Table 30.2.

    The flags a given device type actually reports are given by Table 30.1;
    a driver sets only those and leaves the rest at their defaults.
    """

    ready: bool = False
    unknown: bool = True
    init: bool = False
    power_off: bool = False
    paper_out: bool = False
    paper_jam: bool = False
    disk_error: bool = False
    description: str = ""

    def with_(self, **changes) -> "DriverStatus":
        """A copy with some flags changed."""
        current = {
            "ready": self.ready, "unknown": self.unknown, "init": self.init,
            "power_off": self.power_off, "paper_out": self.paper_out,
            "paper_jam": self.paper_jam, "disk_error": self.disk_error,
            "description": self.description,
        }
        current.update(changes)
        return DriverStatus(**current)

    @property
    def usable(self) -> bool:
        return self.ready and not (self.unknown or self.power_off)


@dataclass
class DriverData:
    """Something the device produced."""

    #: ``barcode``, ``track``, ``ocr``, ``aea``, ``weight``, ``raw``...
    kind: str
    payload: bytes
    attributes: dict = field(default_factory=dict)


#: Callbacks a driver uses to report upward. Both run on the driver's reader
#: thread, so neither may take a platform lock or block.
DataHandler = Callable[[DriverData], None]
StatusHandler = Callable[[DriverStatus], None]


class DeviceDriver(ABC):
    """Base class for every peripheral driver."""

    #: Device types this driver can serve, e.g. ``{"BC", "MS"}``.
    device_types: frozenset[str] = frozenset()

    def __init__(
        self,
        device_name: str,
        device_type: str,
        transport: Transport,
        *,
        on_data: Optional[DataHandler] = None,
        on_status: Optional[StatusHandler] = None,
        poll_interval: float = 0.05,
    ) -> None:
        self.device_name = device_name
        self.device_type = device_type.upper()
        self.transport = transport
        self.on_data = on_data
        self.on_status = on_status
        self.poll_interval = poll_interval

        self._status = DriverStatus()
        self._state_lock = threading.RLock()
        self._reader: Optional[threading.Thread] = None
        self._running = threading.Event()
        # Section 10.4.1: a device starts secured and is only opened up when
        # an application actually holds it.
        self._secured = True
        self._discarded_while_secured = 0

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Open the transport and begin reading."""
        self.transport.open()
        self._set_status(self._status.with_(init=True, unknown=False,
                                            description="initialising"))
        self._running.set()
        self._reader = threading.Thread(
            target=self._read_loop,
            name=f"driver-{self.device_name}",
            daemon=True,
        )
        self._reader.start()
        try:
            self.initialise()
        except (DriverError, TransportError) as exc:
            log.warning("%s failed to initialise: %s", self.device_name, exc)
            self._set_status(
                self._status.with_(init=False, ready=False, unknown=True,
                                   description=str(exc))
            )
            return
        self._set_status(
            self._status.with_(init=False, ready=True, unknown=False,
                               description="ready")
        )

    def stop(self) -> None:
        """Stop reading, secure the device and close the transport."""
        self._running.clear()
        self.secure()
        reader, self._reader = self._reader, None
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2.0)
        try:
            self.transport.close()
        except Exception:  # pragma: no cover - best effort on shutdown
            log.debug("closing %s failed", self.device_name, exc_info=True)
        self._set_status(
            DriverStatus(unknown=True, description="stopped")
        )

    def __enter__(self) -> "DeviceDriver":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- securing (section 10.4.1) ----------------------------------------

    @property
    def secured(self) -> bool:
        with self._state_lock:
            return self._secured

    @property
    def discarded_while_secured(self) -> int:
        """How much input was dropped because the device was secured.

        Worth surfacing: a non-zero count on an idle reader means somebody is
        scanning at an unattended position, which is exactly the case section
        10.4.1 exists to defend against.
        """
        with self._state_lock:
            return self._discarded_while_secured

    def secure(self) -> None:
        """Make the device inoperable, as section 10.4.1 requires."""
        with self._state_lock:
            if self._secured:
                return
            self._secured = True
        log.debug("%s secured", self.device_name)
        self.on_secured()

    def unsecure(self) -> None:
        """Allow the device to be used, because an application holds it."""
        with self._state_lock:
            if not self._secured:
                return
            self._secured = False
        log.debug("%s unsecured", self.device_name)
        self.on_unsecured()

    def on_secured(self) -> None:
        """Hook for drivers that must tell the device itself to stand down."""

    def on_unsecured(self) -> None:
        """Hook for drivers that must re-enable the device."""

    # -- status -----------------------------------------------------------

    @property
    def status(self) -> DriverStatus:
        with self._state_lock:
            return self._status

    def _set_status(self, status: DriverStatus) -> None:
        with self._state_lock:
            if status == self._status:
                return
            self._status = status
        handler = self.on_status
        if handler is not None:
            try:
                handler(status)
            except Exception:  # pragma: no cover - handler is platform code
                log.exception("%s status handler raised", self.device_name)

    # -- data -------------------------------------------------------------

    def _emit(self, data: DriverData) -> None:
        """Hand data upward, unless the device is secured."""
        with self._state_lock:
            if self._secured:
                # Section 10.4.1: input from a secured device is ignored, and
                # never reaches an application.
                self._discarded_while_secured += 1
                log.debug(
                    "%s discarded %d byte(s) read while secured",
                    self.device_name, len(data.payload),
                )
                return
        handler = self.on_data
        if handler is not None:
            try:
                handler(data)
            except Exception:  # pragma: no cover - handler is platform code
                log.exception("%s data handler raised", self.device_name)

    # -- writing ----------------------------------------------------------

    def write(self, data: bytes) -> int:
        """Send bytes to the device, refusing while it is secured."""
        with self._state_lock:
            if self._secured:
                raise DeviceSecured(
                    f"{self.device_name} is secured; no application holds it "
                    f"(section 10.4.1)"
                )
        return self.transport.write(data)

    # -- reading ----------------------------------------------------------

    def _read_loop(self) -> None:
        while self._running.is_set():
            try:
                chunk = self.transport.read()
            except TransportError as exc:
                if self._running.is_set():
                    log.warning("%s read failed: %s", self.device_name, exc)
                    self._set_status(
                        self.status.with_(
                            ready=False, unknown=False, power_off=True,
                            description=f"transport lost: {exc}",
                        )
                    )
                return
            if not chunk:
                continue
            try:
                self.handle_bytes(chunk)
            except Exception:  # pragma: no cover - driver subclass code
                log.exception("%s failed handling input", self.device_name)

    # -- subclass hooks ---------------------------------------------------

    def initialise(self) -> None:
        """Bring the device to a known state. Called once after start."""

    @abstractmethod
    def handle_bytes(self, chunk: bytes) -> None:
        """Consume bytes read from the transport, emitting data as parsed."""

    def test(self) -> DriverStatus:
        """Exercise the device, for the device test screen (section 10.4.3).

        The default only re-reads status; drivers that can actually do
        something -- print a test page, flash an indicator -- override it,
        because section 11.2.1 wants a test that exercises the device rather
        than merely querying it.
        """
        return self.status

    def describe(self) -> str:
        return (
            f"{self.device_name} ({self.device_type}) on "
            f"{self.transport.describe()}"
        )
