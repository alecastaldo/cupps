"""messageID allocation and tracking (TS 01.04.0004 section 27.1.5).

Each connection keeps its own independent messageID sequence.  Applications
allocate from ``MinMessageID .. MinPlatformMsgID - 1``; platforms allocate from
``MinPlatformMsgID .. MaxMessageID``.  Both wrap to the bottom of their own
range on overflow.  Each side also keeps the most recent ``MsgIDListLen`` IDs
sent and received so a response can be matched to its request.
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from typing import Deque, Optional

from . import params


class MessageIdGenerator:
    """Thread-safe allocator for one connection's messageID sequence."""

    def __init__(self, *, platform_side: bool = False) -> None:
        self._platform_side = platform_side
        if platform_side:
            self._low = params.MIN_PLATFORM_MSG_ID
            self._high = params.MAX_MESSAGE_ID
        else:
            self._low = params.MIN_MESSAGE_ID
            self._high = params.MIN_PLATFORM_MSG_ID - 1
        self._next = self._low
        self._lock = threading.Lock()

    @property
    def range(self) -> tuple[int, int]:
        """The inclusive ``(low, high)`` bounds of this side's range."""
        return (self._low, self._high)

    def allocate(self) -> int:
        """Return the next messageID, wrapping within this side's range."""
        with self._lock:
            value = self._next
            self._next = self._low if value >= self._high else value + 1
            return value

    def owns(self, message_id: int) -> bool:
        """True when ``message_id`` falls in this side's allocation range."""
        return self._low <= message_id <= self._high


class MessageIdTracker:
    """Recent-messageID window of length ``MsgIDListLen`` (26.11.23)."""

    def __init__(self, length: int = params.MSG_ID_LIST_LEN) -> None:
        self._length = length
        self._ids: Deque[int] = deque(maxlen=length)
        self._lock = threading.Lock()

    def record(self, message_id: int) -> None:
        with self._lock:
            self._ids.append(message_id)

    def __contains__(self, message_id: object) -> bool:
        with self._lock:
            return message_id in self._ids

    def __len__(self) -> int:
        with self._lock:
            return len(self._ids)


class PendingRequests:
    """Outstanding request registry, bounded by ``PltStreamOutMsgs`` (26.11.35).

    The interface permits overlapping messages, so a response is matched back
    to its request through the messageID it echoes.  ``limit`` caps how many
    requests may be in flight on one socket; ZL device sessions raise it to
    ``PltStreamOutMsgsZL`` (26.11.36).
    """

    def __init__(self, limit: int = params.PLT_STREAM_OUT_MSGS) -> None:
        self._limit = limit
        self._pending: "OrderedDict[int, object]" = OrderedDict()
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)

    def has_capacity(self) -> bool:
        with self._lock:
            return len(self._pending) < self._limit

    def add(self, message_id: int, slot: object) -> None:
        """Register an in-flight request.

        Raises :class:`OverflowError` when the peer's outstanding-message
        allowance is already spent; the caller must wait rather than exceed it.
        """
        with self._lock:
            if len(self._pending) >= self._limit:
                raise OverflowError(
                    f"{len(self._pending)} outstanding messages already in "
                    f"flight (limit {self._limit})"
                )
            self._pending[message_id] = slot

    def pop(self, message_id: int) -> Optional[object]:
        """Retrieve and clear the slot awaiting ``message_id``, if any."""
        with self._lock:
            return self._pending.pop(message_id, None)

    def drain(self) -> list[object]:
        """Remove and return every waiting slot, for use when a session dies."""
        with self._lock:
            slots = list(self._pending.values())
            self._pending.clear()
            return slots
