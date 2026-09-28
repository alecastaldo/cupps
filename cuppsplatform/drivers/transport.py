"""Byte transports for physical peripherals.

A transport moves bytes; it knows nothing about CUPPS.  Separating it from
the protocol matters because the same AEA boarding pass printer may be on a
serial port at one station and a TCP socket at the next, and the AEA driver
should not care which.

Four implementations ship:

``SerialTransport``
    RS-232 and USB serial, via pyserial.  Section 6.3.20 requires COM
    numbering to 255, and Windows needs the ``\\\\.\\COMxyz`` form above COM9,
    which :func:`normalise_port` applies so callers can just write ``COM17``.
``TcpTransport``
    Networked printers, gates and scales.
``LoopbackTransport``
    In-process, for the simulator and for tests.
``PtyTransport``
    A pseudo-terminal pair.  A PTY behaves like a serial port to pyserial, so
    the serial path can be exercised end to end in CI with no hardware
    attached -- which is the difference between a driver that is tested and
    one that is merely written.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("cuppsplatform.drivers.transport")

#: Windows requires the device-namespace form for COM10 and above.
_COM_RE = re.compile(r"^COM(\d+)$", re.IGNORECASE)


class TransportError(IOError):
    """The transport could not be opened, read or written."""


def normalise_port(port: str) -> str:
    """Return the form the operating system needs for ``port``.

    ``COM9`` is fine as-is; ``COM10`` and above must be opened as
    ``\\\\.\\COM10`` on Windows (section 6.3.20).  Anything that is not a bare
    ``COMnn`` -- a POSIX device path, an already-escaped name -- is returned
    unchanged.
    """
    match = _COM_RE.match(port.strip())
    if not match:
        return port
    number = int(match.group(1))
    if number <= 9:
        return f"COM{number}"
    return rf"\\.\COM{number}"


@dataclass
class TransportConfig:
    """How to reach one physical device."""

    kind: str                      # serial | tcp | loopback | pty
    port: str = ""                 # serial port name, or unused
    host: str = ""                 # tcp
    tcp_port: int = 0              # tcp
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    #: Read timeout in seconds. Kept short: the driver polls in a loop and a
    #: long timeout would delay a clean shutdown.
    read_timeout: float = 0.2
    write_timeout: float = 5.0
    rtscts: bool = False
    xonxoff: bool = False
    extra: dict = field(default_factory=dict)


class Transport(ABC):
    """A bidirectional byte stream to one device."""

    def __init__(self, config: TransportConfig) -> None:
        self.config = config
        self._lock = threading.RLock()

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def write(self, data: bytes) -> int: ...

    @abstractmethod
    def read(self, size: int = 4096) -> bytes:
        """Read up to ``size`` bytes, returning ``b""`` on timeout."""

    def describe(self) -> str:
        return f"{self.config.kind}:{self.config.port or self.config.host}"

    def __enter__(self) -> "Transport":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class SerialTransport(Transport):
    """RS-232 or USB serial, via pyserial."""

    def __init__(self, config: TransportConfig) -> None:
        super().__init__(config)
        self._serial = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def open(self) -> None:
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - depends on install
            raise TransportError(
                "serial devices need pyserial; install it with "
                "'pip install pyserial' or 'pip install .[serial]'"
            ) from exc

        port = normalise_port(self.config.port)
        try:
            self._serial = serial.Serial(
                port=port,
                baudrate=self.config.baudrate,
                bytesize=self.config.bytesize,
                parity=self.config.parity,
                stopbits=self.config.stopbits,
                timeout=self.config.read_timeout,
                write_timeout=self.config.write_timeout,
                rtscts=self.config.rtscts,
                xonxoff=self.config.xonxoff,
            )
        except Exception as exc:
            raise TransportError(f"could not open {port}: {exc}") from exc
        log.info("opened serial %s at %d baud", port, self.config.baudrate)

    def close(self) -> None:
        with self._lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:  # pragma: no cover - best effort
                    log.debug("error closing %s", self.describe(), exc_info=True)
                self._serial = None

    def write(self, data: bytes) -> int:
        with self._lock:
            if not self.is_open:
                raise TransportError(f"{self.describe()} is not open")
            try:
                written = self._serial.write(data)
                self._serial.flush()
                return written or 0
            except Exception as exc:
                raise TransportError(f"write to {self.describe()} failed: {exc}") from exc

    def read(self, size: int = 4096) -> bytes:
        if not self.is_open:
            raise TransportError(f"{self.describe()} is not open")
        try:
            # in_waiting lets a burst be taken in one read rather than one
            # byte at a time, without ever blocking past the read timeout.
            waiting = self._serial.in_waiting
            return self._serial.read(max(1, min(size, waiting or 1)))
        except Exception as exc:
            raise TransportError(f"read from {self.describe()} failed: {exc}") from exc

    def describe(self) -> str:
        return f"serial:{normalise_port(self.config.port)}"


class TcpTransport(Transport):
    """A device reached over TCP."""

    def __init__(self, config: TransportConfig) -> None:
        super().__init__(config)
        self._socket: Optional[socket.socket] = None

    @property
    def is_open(self) -> bool:
        return self._socket is not None

    def open(self) -> None:
        try:
            sock = socket.create_connection(
                (self.config.host, self.config.tcp_port),
                timeout=self.config.write_timeout,
            )
        except OSError as exc:
            raise TransportError(
                f"could not connect to {self.config.host}:"
                f"{self.config.tcp_port}: {exc}"
            ) from exc
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.settimeout(self.config.read_timeout)
        self._socket = sock
        log.info("opened tcp %s", self.describe())

    def close(self) -> None:
        with self._lock:
            sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def write(self, data: bytes) -> int:
        with self._lock:
            sock = self._socket
            if sock is None:
                raise TransportError(f"{self.describe()} is not open")
            try:
                sock.sendall(data)
                return len(data)
            except OSError as exc:
                raise TransportError(f"write to {self.describe()} failed: {exc}") from exc

    def read(self, size: int = 4096) -> bytes:
        sock = self._socket
        if sock is None:
            raise TransportError(f"{self.describe()} is not open")
        try:
            return sock.recv(size)
        except socket.timeout:
            return b""
        except OSError as exc:
            raise TransportError(f"read from {self.describe()} failed: {exc}") from exc

    def describe(self) -> str:
        return f"tcp:{self.config.host}:{self.config.tcp_port}"


class LoopbackTransport(Transport):
    """An in-process transport, for the simulator and for tests.

    Whatever the platform writes is available to :meth:`take_written`, and
    :meth:`feed` makes bytes readable as though the device had sent them.
    """

    def __init__(self, config: Optional[TransportConfig] = None) -> None:
        super().__init__(config or TransportConfig(kind="loopback"))
        self._open = False
        self._inbound = bytearray()
        self._outbound = bytearray()
        self._data_ready = threading.Event()

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False
        self._data_ready.set()

    def write(self, data: bytes) -> int:
        with self._lock:
            if not self._open:
                raise TransportError("loopback transport is not open")
            self._outbound += data
            return len(data)

    def read(self, size: int = 4096) -> bytes:
        if not self._open:
            raise TransportError("loopback transport is not open")
        # Mirror a real read timeout so a driver's poll loop behaves the same.
        self._data_ready.wait(timeout=self.config.read_timeout)
        with self._lock:
            chunk = bytes(self._inbound[:size])
            del self._inbound[: len(chunk)]
            if not self._inbound:
                self._data_ready.clear()
            return chunk

    # -- test and simulator controls --------------------------------------

    def feed(self, data: bytes) -> None:
        """Make ``data`` readable, as though the device had sent it."""
        with self._lock:
            self._inbound += data
        self._data_ready.set()

    def take_written(self) -> bytes:
        """Take everything the platform has written since the last call."""
        with self._lock:
            written, self._outbound = bytes(self._outbound), bytearray()
            return written

    @property
    def written(self) -> bytes:
        with self._lock:
            return bytes(self._outbound)

    def describe(self) -> str:
        return f"loopback:{self.config.port or 'anonymous'}"


class PtyTransport(SerialTransport):
    """A serial transport over a pseudo-terminal pair.

    A PTY presents a real character device, so pyserial drives it exactly as
    it drives a COM port.  That makes the serial code path testable in CI --
    the same class, the same reads and writes, no hardware -- and gives a
    bench harness a way to stand in for a printer that has not arrived yet.

    :meth:`device_write` and :meth:`device_read` act as the far end.
    """

    def __init__(self, config: Optional[TransportConfig] = None) -> None:
        super().__init__(config or TransportConfig(kind="pty"))
        self._master: Optional[int] = None
        self._slave: Optional[int] = None

    def open(self) -> None:
        import pty

        self._master, self._slave = pty.openpty()
        self.config.port = os.ttyname(self._slave)
        super().open()

    def close(self) -> None:
        super().close()
        for handle_name in ("_master", "_slave"):
            handle = getattr(self, handle_name)
            if handle is not None:
                try:
                    os.close(handle)
                except OSError:
                    pass
                setattr(self, handle_name, None)

    def device_write(self, data: bytes) -> None:
        """Send bytes to the platform, as the peripheral would."""
        if self._master is None:
            raise TransportError("pty is not open")
        os.write(self._master, data)

    def device_read(self, size: int = 4096, timeout: float = 1.0) -> bytes:
        """Read what the platform sent, as the peripheral would."""
        import select

        if self._master is None:
            raise TransportError("pty is not open")
        ready, _, _ = select.select([self._master], [], [], timeout)
        if not ready:
            return b""
        return os.read(self._master, size)

    def describe(self) -> str:
        return f"pty:{self.config.port or 'unopened'}"


#: Transport kinds, for binding from configuration.
TRANSPORTS = {
    "serial": SerialTransport,
    "tcp": TcpTransport,
    "loopback": LoopbackTransport,
    "pty": PtyTransport,
}


def build_transport(config: TransportConfig) -> Transport:
    """Construct the transport a configuration names."""
    try:
        factory = TRANSPORTS[config.kind]
    except KeyError:
        raise TransportError(
            f"unknown transport kind {config.kind!r}; known kinds are "
            f"{sorted(TRANSPORTS)}"
        ) from None
    return factory(config)
