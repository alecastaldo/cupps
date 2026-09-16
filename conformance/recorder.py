"""Conversation recording for Application Compliance Testing.

Section 13.1.2 describes compliance testing as comparing "actual testing
(i.e. logged) results to standard expected results", and Figure 12.1 names the
artefacts a Compliance Testing Record "and associated logs".  This module
produces that log: every message in both directions, attributed to a
connection, with the timing a timing check needs.

Nothing here judges anything.  The recording is evidence; :mod:`checks`
applies the rules to it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Optional

from cupps import xmlmsg


@dataclass
class RecordedMessage:
    """One message observed on the wire."""

    connection_id: int
    direction: str          # "in" (application to platform) or "out"
    message_name: str
    message_id: int
    #: Seconds since the recording started, for interval checks.
    at: float
    wall_clock: str
    namespace: Optional[str]
    body: xmlmsg.Element
    raw_length: int

    def attr(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.body.get(name, default)

    @property
    def is_from_application(self) -> bool:
        return self.direction == "in"


@dataclass
class Connection:
    """One socket's life, with the conversation that happened on it."""

    connection_id: int
    is_device_connection: bool = False
    opened_at: float = 0.0
    closed_at: Optional[float] = None
    #: Device name, once <deviceAcquireRequest> names one.
    device_name: str = ""
    device_type: str = ""
    interface_mode: str = ""
    interface_level: str = ""
    messages: list[RecordedMessage] = field(default_factory=list)
    faults: list[tuple[str, str]] = field(default_factory=list)

    @property
    def closed(self) -> bool:
        return self.closed_at is not None

    def inbound(self, *names: str) -> list[RecordedMessage]:
        """Application-originated messages, optionally filtered by name."""
        return [
            message
            for message in self.messages
            if message.is_from_application
            and (not names or message.message_name in names)
        ]

    def outbound(self, *names: str) -> list[RecordedMessage]:
        """Platform-originated messages, optionally filtered by name."""
        return [
            message
            for message in self.messages
            if not message.is_from_application
            and (not names or message.message_name in names)
        ]

    def first_inbound(self) -> Optional[RecordedMessage]:
        for message in self.messages:
            if message.is_from_application:
                return message
        return None

    @property
    def label(self) -> str:
        if self.device_name:
            return f"device {self.device_name}"
        return "platform" if not self.is_device_connection else "device (unacquired)"


class Recorder:
    """Collects the conversation from an instrumented platform."""

    def __init__(self) -> None:
        self._started = time.monotonic()
        self._lock = threading.Lock()
        self.connections: dict[int, Connection] = {}
        #: Device tokens the platform issued, in order, so a check can tell
        #: whether the application reused an invalidated one (section 26.7).
        self.issued_tokens: list[str] = []
        #: Tokens the platform has invalidated (bye, or connection lost),
        #: with the time it happened, so a check can tell a legitimate early
        #: use of a token from a reuse after invalidation.
        self.invalidated_tokens: dict[str, float] = {}
        #: Connections the harness tore down itself. The application never got
        #: the chance to close these politely, so closing-handshake checks
        #: must not hold them against it.
        self.harness_closed: set[int] = set()
        #: Free-form notes from the scenario driver, interleaved by time.
        self.notes: list[tuple[float, str]] = []

    # -- recording --------------------------------------------------------

    def _now(self) -> float:
        return time.monotonic() - self._started

    def _connection(self, peer) -> Connection:
        with self._lock:
            connection = self.connections.get(peer.connection_id)
            if connection is None:
                connection = Connection(
                    connection_id=peer.connection_id, opened_at=self._now()
                )
                self.connections[peer.connection_id] = connection
            return connection

    def record_open(self, peer) -> None:
        self._connection(peer)

    def record_close(self, peer) -> None:
        connection = self._connection(peer)
        connection.closed_at = self._now()

    def record_fault(self, peer, kind: str, detail: str) -> None:
        self._connection(peer).faults.append((kind, detail))

    def record_inbound(self, peer, message: xmlmsg.Message) -> None:
        self._record(peer, message, "in")

    def record_outbound(self, peer, message: xmlmsg.Message) -> None:
        self._record(peer, message, "out")

    def _record(self, peer, message: xmlmsg.Message, direction: str) -> None:
        connection = self._connection(peer)
        connection.is_device_connection = bool(
            getattr(peer, "is_device_connection", False)
        ) or connection.is_device_connection

        encoded = message.encode()
        record = RecordedMessage(
            connection_id=connection.connection_id,
            direction=direction,
            message_name=message.message_name,
            message_id=message.message_id,
            at=self._now(),
            wall_clock=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            namespace=message.namespace,
            body=message.body,
            raw_length=len(encoded),
        )
        with self._lock:
            connection.messages.append(record)

        # Track the few facts later checks need but cannot cheaply re-derive.
        if direction == "in":
            if message.message_name == "deviceAcquireRequest":
                connection.device_name = message.body.get("deviceName", "") or ""
                connection.is_device_connection = True
            elif message.message_name == "interfaceModeRequest":
                connection.interface_mode = message.body.get("mode", "") or ""
            elif message.message_name == "interfaceLevelRequest":
                connection.interface_level = message.body.get("level", "") or ""
        else:
            if message.message_name == "authenticateResponse":
                token = message.body.get("deviceToken", "") or ""
                if token:
                    with self._lock:
                        self.issued_tokens.append(token)
            elif message.message_name == "byeResponse":
                with self._lock:
                    for token in self.issued_tokens:
                        self.invalidated_tokens.setdefault(token, record.at)

    def note(self, text: str) -> None:
        """Record a scenario step, so the report reads as a narrative."""
        with self._lock:
            self.notes.append((self._now(), text))

    def invalidate_all_tokens(self) -> None:
        """Mark every issued token dead, as a platform disconnect would."""
        at = self._now()
        with self._lock:
            for token in self.issued_tokens:
                self.invalidated_tokens.setdefault(token, at)

    def mark_harness_closed(self, connection_ids) -> None:
        """Record that the harness, not the application, closed these."""
        with self._lock:
            self.harness_closed.update(connection_ids)

    # -- access -----------------------------------------------------------

    def ordered(self) -> list[Connection]:
        with self._lock:
            return sorted(self.connections.values(), key=lambda c: c.opened_at)

    def platform_connections(self) -> list[Connection]:
        return [c for c in self.ordered() if not c.is_device_connection]

    def device_connections(self) -> list[Connection]:
        return [c for c in self.ordered() if c.is_device_connection]

    def all_messages(self) -> Iterator[RecordedMessage]:
        for connection in self.ordered():
            yield from connection.messages

    @property
    def message_count(self) -> int:
        return sum(len(c.messages) for c in self.ordered())

    @property
    def duration(self) -> float:
        return self._now()
