"""Exceptions raised by the CUPPS client."""

from __future__ import annotations

from typing import Optional


class CuppsError(Exception):
    """Base class for every fault raised by this library."""


class ConnectionClosed(CuppsError):
    """The peer closed the socket, or the session was torn down locally."""


class SessionError(CuppsError):
    """The peer sent ``<sessionErrorEvent>``; the socket is closed (26.6)."""

    def __init__(self, event_type: str, detail: str = "") -> None:
        super().__init__(f"sessionErrorEvent eventType={event_type!r} {detail}".strip())
        self.event_type = event_type
        self.detail = detail


class IllogicalMessage(CuppsError):
    """The peer sent ``<illogicalMessageErrorEvent>`` (section 27.1.2).

    The connection is closed by the sender of the event.  A platform
    connection carrying this error also invalidates the device token, so every
    device session must be re-established after reconnecting.
    """

    def __init__(self, expected: Optional[list[str]] = None, detail: str = "") -> None:
        expected = expected or []
        message = "illogicalMessageErrorEvent"
        if expected:
            message += f"; expected one of {', '.join(expected)}"
        if detail:
            message += f"; {detail}"
        super().__init__(message)
        self.expected_message_names = expected
        self.detail = detail


class RequestFailed(CuppsError):
    """A request returned a ``result`` other than OK."""

    def __init__(self, message_name: str, result: str, detail: str = "") -> None:
        super().__init__(
            f"{message_name} returned result={result!r}"
            + (f": {detail}" if detail else "")
        )
        self.message_name = message_name
        self.result = result
        self.detail = detail


class RequestTimeout(CuppsError):
    """No response arrived inside the deadline the spec sets for the request."""


class DeviceLocked(RequestFailed):
    """A lock request was refused because another session holds the device."""

    def __init__(self, locker: Optional[dict] = None) -> None:
        detail = ""
        if locker:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(locker.items()))
        super().__init__("deviceLockRequest", "deviceLocked", detail)
        self.locker = locker or {}


class TokenInvalidated(CuppsError):
    """The device token is no longer valid (section 26.7).

    Raised when the platform connection dropped or ``<byeRequest>`` was sent;
    every device session opened with that token must be rebuilt after a fresh
    ``<authenticateRequest>``.
    """
