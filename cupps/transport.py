"""CUPPS streaming transport (TS 01.04.0004 sections 26.9, 27.1.3, 27.1.4).

A :class:`Connection` owns one TCP socket and implements the streaming
protocol on top of it: header-then-body framing, the inter-byte
``PltStreamAccumTime`` timer, TCP keep-alive, FIFO message ordering, and the
overlapped-request window bounded by ``PltStreamOutMsgs``.

A single reader thread drains the socket so notifications -- which the
platform may send at any time (section 27.1.4) -- are delivered promptly
instead of waiting behind a request/response exchange.
"""

from __future__ import annotations

import base64
import logging
import socket
import threading
from typing import Callable, Optional

from . import header as hdr
from . import params, xmlmsg
from .errors import (
    ConnectionClosed,
    IllogicalMessage,
    RequestTimeout,
    SessionError,
)
from .msgid import MessageIdGenerator, MessageIdTracker, PendingRequests

log = logging.getLogger("cupps.transport")

#: Notification and error messages the platform sends unsolicited.
_UNSOLICITED = frozenset({"notify", "sessionErrorEvent", "illogicalMessageErrorEvent"})

NotificationHandler = Callable[[xmlmsg.Message], None]


class _ResponseSlot:
    """A rendezvous for one in-flight request."""

    __slots__ = ("event", "message", "failure")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.message: Optional[xmlmsg.Message] = None
        self.failure: Optional[BaseException] = None

    def complete(self, message: xmlmsg.Message) -> None:
        self.message = message
        self.event.set()

    def fail(self, error: BaseException) -> None:
        self.failure = error
        self.event.set()


class Connection:
    """One framed, message-oriented CUPPS socket."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        connect_timeout: float = 10.0,
        outstanding_limit: int = params.PLT_STREAM_OUT_MSGS,
        name: str = "cupps",
    ) -> None:
        self.host = host
        self.port = port
        self.name = name
        self._sock: Optional[socket.socket] = None
        self._connect_timeout = connect_timeout
        self._ids = MessageIdGenerator(platform_side=False)
        self._sent = MessageIdTracker()
        self._received = MessageIdTracker()
        self._pending = PendingRequests(limit=outstanding_limit)
        self._send_lock = threading.Lock()
        self._capacity = threading.Condition()
        self._reader: Optional[threading.Thread] = None
        self._closed = threading.Event()
        self._interface_level = "01.04"
        self.on_notification: Optional[NotificationHandler] = None
        #: Set when the peer tore the session down; surfaced to every waiter.
        self.teardown_reason: Optional[BaseException] = None

    # -- lifecycle --------------------------------------------------------

    def connect(self) -> None:
        """Open the socket and start the reader thread."""
        sock = socket.create_connection(
            (self.host, self.port), timeout=self._connect_timeout
        )
        # Section 27.1.3 requires keep-alive on every application/platform
        # socket so a broken link is noticed rather than hanging forever.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        _tune_keepalive(sock)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # PltStreamAccumTime (26.11.34) bounds the gap between bytes, and the
        # reader re-arms it per byte, so it is the right socket timeout.
        sock.settimeout(params.with_tolerance(params.PLT_STREAM_ACCUM_TIME))
        self._sock = sock
        self._closed.clear()
        self.teardown_reason = None
        self._reader = threading.Thread(
            target=self._read_loop, name=f"{self.name}-reader", daemon=True
        )
        self._reader.start()
        log.debug("%s connected to %s:%s", self.name, self.host, self.port)

    @property
    def connected(self) -> bool:
        return self._sock is not None and not self._closed.is_set()

    @property
    def interface_level(self) -> str:
        return self._interface_level

    @interface_level.setter
    def interface_level(self, level: str) -> None:
        self._interface_level = level

    def close(self) -> None:
        """Close the socket and release every waiter."""
        self._closed.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self._release_waiters(self.teardown_reason or ConnectionClosed("closed locally"))
        with self._capacity:
            self._capacity.notify_all()

    def __enter__(self) -> "Connection":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- sending ----------------------------------------------------------

    def allocate_message_id(self) -> int:
        return self._ids.allocate()

    def send_request(
        self,
        message: xmlmsg.Message,
        *,
        timeout: float,
    ) -> xmlmsg.Message:
        """Send a request and block until its matching response arrives.

        ``timeout`` is the per-message ceiling the spec sets for this exchange
        (the ``*MaxTime`` parameters of section 26.11); it is widened by the
        1% tolerance section 26.11 mandates.
        """
        slot = _ResponseSlot()
        deadline = params.with_tolerance(timeout)

        # Respect PltStreamOutMsgs (26.11.35): never put more requests on the
        # wire than the peer has agreed to hold.
        with self._capacity:
            while not self._pending.has_capacity():
                if not self.connected:
                    raise ConnectionClosed("connection closed while waiting to send")
                if not self._capacity.wait(timeout=deadline):
                    raise RequestTimeout(
                        f"{message.message_name}: no capacity within {deadline:.1f}s "
                        f"(PltStreamOutMsgs={self._pending.limit})"
                    )
            self._pending.add(message.message_id, slot)

        try:
            self._write(message)
        except BaseException:
            self._pending.pop(message.message_id)
            self._notify_capacity()
            raise

        if not slot.event.wait(timeout=deadline):
            self._pending.pop(message.message_id)
            self._notify_capacity()
            raise RequestTimeout(
                f"{message.message_name} (messageID={message.message_id}) "
                f"got no response within {deadline:.1f}s"
            )
        if slot.failure is not None:
            raise slot.failure
        assert slot.message is not None
        return slot.message

    def send_notification(self, message: xmlmsg.Message) -> None:
        """Send a message that expects no response."""
        self._write(message)

    def _write(self, message: xmlmsg.Message) -> None:
        body = message.encode()
        if len(body) > params.PLT_MAX_MSG_SIZE:
            raise hdr.HeaderLengthError(
                f"{message.message_name} body is {len(body)} bytes, over "
                f"PltMaxMsgSize ({params.PLT_MAX_MSG_SIZE})"
            )
        frame = hdr.frame(body)
        with self._send_lock:
            sock = self._sock
            if sock is None or self._closed.is_set():
                raise ConnectionClosed("socket is not open")
            try:
                sock.sendall(frame)
            except OSError as exc:
                self._fail_session(ConnectionClosed(f"send failed: {exc}"))
                raise ConnectionClosed(f"send failed: {exc}") from exc
        self._sent.record(message.message_id)
        log.debug("%s >> %s id=%s", self.name, message.message_name, message.message_id)

    # -- receiving --------------------------------------------------------

    def _read_loop(self) -> None:
        try:
            while not self._closed.is_set():
                message = self._read_message()
                if message is None:
                    break
                self._dispatch(message)
        except SessionError as exc:
            log.warning("%s session error: %s", self.name, exc)
            self._fail_session(exc)
        except ConnectionClosed as exc:
            log.debug("%s closed: %s", self.name, exc)
            self._fail_session(exc)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("%s reader failed", self.name)
            self._fail_session(ConnectionClosed(str(exc)))
        finally:
            self._closed.set()
            self._notify_capacity()

    def _read_message(self) -> Optional[xmlmsg.Message]:
        raw_header = self._read_header_bytes()
        if raw_header is None:
            return None
        try:
            head = hdr.decode_header(raw_header)
        except hdr.HeaderError as exc:
            self._send_session_error(exc.event_type)
            raise SessionError(exc.event_type, str(exc)) from exc

        body = self._read_exactly(head.body_length, on_timeout="bodyTimeout")
        if body is None:
            return None
        if head.is_error:
            # Peer flagged the frame itself as an error (Listing 26.1).
            raise SessionError("headerVersion", body.decode("utf-8", "replace"))
        try:
            return xmlmsg.parse(body)
        except xmlmsg.MessageParseError as exc:
            self._send_session_error("bodyParseFailure", body=body)
            raise SessionError("bodyParseFailure", str(exc)) from exc

    def _read_header_bytes(self) -> Optional[bytes]:
        """Read a header one byte at a time, stopping on the first bad byte.

        Section 26.9 requires character-by-character reading with immediate
        termination on an invalid character, so a peer that starts talking
        gibberish is cut off without consuming a whole frame's worth of bytes.
        """
        collected = bytearray()
        sock = self._sock
        if sock is None:
            return None
        while len(collected) < hdr.HEADER_01_SIZE:
            try:
                chunk = sock.recv(1)
            except socket.timeout as exc:
                if not collected:
                    # Idle between messages rather than mid-header. The socket
                    # is simply quiet; let the caller decide when to give up.
                    if self._closed.is_set():
                        return None
                    continue
                self._send_session_error("headerTimeout")
                raise SessionError(
                    "headerTimeout",
                    f"only {len(collected)} of {hdr.HEADER_01_SIZE} header bytes",
                ) from exc
            except OSError as exc:
                raise ConnectionClosed(f"recv failed: {exc}") from exc
            if not chunk:
                if collected:
                    raise ConnectionClosed("peer closed mid-header")
                return None
            if not hdr.is_header_character(chunk[0]):
                self._send_session_error("headerVersion")
                raise SessionError(
                    "headerVersion",
                    f"invalid header byte {chunk[0]:#04x} at offset {len(collected)}",
                )
            collected += chunk
        return bytes(collected)

    def _read_exactly(self, count: int, *, on_timeout: str) -> Optional[bytes]:
        buffer = bytearray()
        sock = self._sock
        if sock is None:
            return None
        while len(buffer) < count:
            try:
                chunk = sock.recv(min(65536, count - len(buffer)))
            except socket.timeout as exc:
                # PltStreamAccumTime elapsed between bytes (26.11.34).
                self._send_session_error(on_timeout)
                raise SessionError(
                    on_timeout,
                    f"read {len(buffer)} of {count} body bytes before timeout",
                ) from exc
            except OSError as exc:
                raise ConnectionClosed(f"recv failed: {exc}") from exc
            if not chunk:
                raise ConnectionClosed("peer closed mid-body")
            buffer += chunk
        return bytes(buffer)

    def _dispatch(self, message: xmlmsg.Message) -> None:
        self._received.record(message.message_id)
        log.debug("%s << %s id=%s", self.name, message.message_name, message.message_id)

        if message.message_name == "illogicalMessageErrorEvent":
            expected = [
                child.get("messageName") or ""
                for child in message.body.iter("expectedMessageName")
            ]
            error = IllogicalMessage(expected=[e for e in expected if e])
            self._fail_session(error)
            raise SessionError("illogicalMessage", str(error))

        if message.message_name == "sessionErrorEvent":
            event_type = message.body.get("eventType", "unknown")
            raise SessionError(event_type, "peer reported a session error")

        slot = self._pending.pop(message.message_id)
        if slot is not None:
            self._notify_capacity()
            slot.complete(message)  # type: ignore[attr-defined]
            return

        if message.message_name in _UNSOLICITED or self._is_platform_originated(message):
            handler = self.on_notification
            if handler is not None:
                try:
                    handler(message)
                except Exception:  # pragma: no cover - handler is app code
                    log.exception("%s notification handler raised", self.name)
            return

        log.warning(
            "%s discarding unmatched %s id=%s",
            self.name,
            message.message_name,
            message.message_id,
        )

    @staticmethod
    def _is_platform_originated(message: xmlmsg.Message) -> bool:
        """True when the messageID falls in the platform's range (27.1.5)."""
        return message.message_id >= params.MIN_PLATFORM_MSG_ID

    # -- teardown helpers -------------------------------------------------

    def _send_session_error(self, event_type: str, body: Optional[bytes] = None) -> None:
        """Best-effort ``<sessionErrorEvent>`` before dropping the socket.

        Section 26.6 requires the offending body to be carried in Base64 for a
        ``bodyParseFailure``.
        """
        sock = self._sock
        if sock is None:
            return
        element = xmlmsg.Element("sessionErrorEvent", {"eventType": event_type})
        if body is not None:
            element.add(
                xmlmsg.Element(
                    "body", text=base64.b64encode(body).decode("ascii")
                )
            )
        message = xmlmsg.build(
            "sessionErrorEvent",
            self.allocate_message_id(),
            body=element,
            interface_level=self._interface_level,
        )
        try:
            with self._send_lock:
                sock.sendall(hdr.frame(message.encode()))
        except OSError:
            pass

    def _fail_session(self, error: BaseException) -> None:
        self.teardown_reason = error
        self._closed.set()
        self._release_waiters(error)

    def _release_waiters(self, error: BaseException) -> None:
        for slot in self._pending.drain():
            slot.fail(error)  # type: ignore[attr-defined]
        self._notify_capacity()

    def _notify_capacity(self) -> None:
        with self._capacity:
            self._capacity.notify_all()


def _tune_keepalive(sock: socket.socket) -> None:
    """Shorten keep-alive probing where the platform exposes the knobs.

    Section 27.1.3 mandates keep-alive but sets no intervals, and the system
    defaults are two hours.  That matters more than it first appears: a peer
    that vanishes without sending a FIN -- a switch failover, a platform
    losing power, a firewall dropping the flow, all routine in an airport --
    leaves a *half-open* connection that a blocked reader never notices. The
    application would sit in aStd with the agent screen showing "Connected"
    while nothing worked.

    These values detect that within roughly 20 + 3x5 = 35 seconds, at a cost
    of a few probe packets per minute per socket. The alternative, polling the
    platform to prove it is alive, is what section 30.2 calls a bug.
    """
    for option, value in (
        ("TCP_KEEPIDLE", 20),
        ("TCP_KEEPINTVL", 5),
        ("TCP_KEEPCNT", 3),
    ):
        constant = getattr(socket, option, None)
        if constant is None:
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, constant, value)
        except OSError:
            pass
