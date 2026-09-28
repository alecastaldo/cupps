"""Time and timers for the platform.

The platform's behaviour is largely defined by deadlines: a device lock
expires after ``DevLkdTime``, a user is timed out after ``UsrToTime``, an
application that will not stop within ``AppSpgTime`` becomes a zombie.  Those
values are 60, 600 and 45 seconds, so a test suite that used real time would
take hours and would be flaky at that.

Every timer therefore goes through a :class:`Clock`.  Production uses
:class:`RealClock`; tests use :class:`ManualClock` and advance time
explicitly, which makes the lifecycle deterministic -- a lock either expires
at exactly ``DevLkdTime`` or the test fails.

Section 26.11 requires a run-time tolerance of plus or minus one percent on
time-based parameters, which :func:`cupps.params.with_tolerance` applies; the
clock itself is exact and the tolerance is the caller's to apply where the
specification asks for it.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger("cuppsplatform.clock")


@dataclass(order=True)
class _ScheduledCall:
    due: float
    sequence: int
    callback: Callable[[], None] = field(compare=False)
    name: str = field(default="", compare=False)
    cancelled: bool = field(default=False, compare=False)


class Timer:
    """A handle on one scheduled call."""

    __slots__ = ("_entry", "_clock")

    def __init__(self, entry: _ScheduledCall, clock: "Clock") -> None:
        self._entry = entry
        self._clock = clock

    @property
    def name(self) -> str:
        return self._entry.name

    @property
    def due(self) -> float:
        return self._entry.due

    @property
    def cancelled(self) -> bool:
        return self._entry.cancelled

    def cancel(self) -> None:
        """Cancel the call if it has not already run."""
        self._entry.cancelled = True


class Clock:
    """Base class: a source of time and a scheduler."""

    def now(self) -> float:
        raise NotImplementedError

    def schedule(
        self, delay: float, callback: Callable[[], None], *, name: str = ""
    ) -> Timer:
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        """Release any resources. Safe to call more than once."""


class RealClock(Clock):
    """Wall-clock time with a single scheduler thread.

    One thread rather than a ``threading.Timer`` per deadline: a busy platform
    holds a timer for every device lock, every idle user and every stopping
    application at once, and a thread each does not scale.
    """

    def __init__(self) -> None:
        self._heap: list[_ScheduledCall] = []
        self._lock = threading.Lock()
        self._wakeup = threading.Condition(self._lock)
        self._sequence = itertools.count()
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="cuppsplatform-clock", daemon=True
        )
        self._thread.start()

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def schedule(
        self, delay: float, callback: Callable[[], None], *, name: str = ""
    ) -> Timer:
        entry = _ScheduledCall(
            due=self.now() + max(0.0, delay),
            sequence=next(self._sequence),
            callback=callback,
            name=name,
        )
        with self._wakeup:
            heapq.heappush(self._heap, entry)
            self._wakeup.notify()
        return Timer(entry, self)

    def _run(self) -> None:
        while True:
            with self._wakeup:
                if not self._running:
                    return
                if not self._heap:
                    self._wakeup.wait(timeout=1.0)
                    continue
                entry = self._heap[0]
                if entry.cancelled:
                    heapq.heappop(self._heap)
                    continue
                remaining = entry.due - self.now()
                if remaining > 0:
                    self._wakeup.wait(timeout=min(remaining, 1.0))
                    continue
                heapq.heappop(self._heap)
            # Run outside the lock so a callback may schedule more work.
            _fire(entry)

    def stop(self) -> None:
        with self._wakeup:
            self._running = False
            self._wakeup.notify_all()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)


class ManualClock(Clock):
    """A clock that only moves when a test moves it.

    ``advance`` runs every callback that comes due, in order, including any
    scheduled by an earlier callback within the same advance -- so a cascade
    such as dSpg timing out into dZom resolves in one call.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self._heap: list[_ScheduledCall] = []
        self._sequence = itertools.count()
        self._lock = threading.RLock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        """Advance instead of blocking."""
        self.advance(seconds)

    def schedule(
        self, delay: float, callback: Callable[[], None], *, name: str = ""
    ) -> Timer:
        with self._lock:
            entry = _ScheduledCall(
                due=self._now + max(0.0, delay),
                sequence=next(self._sequence),
                callback=callback,
                name=name,
            )
            heapq.heappush(self._heap, entry)
            return Timer(entry, self)

    def advance(self, seconds: float) -> int:
        """Move time forward, firing what comes due. Returns how many ran."""
        if seconds < 0:
            raise ValueError("time does not run backwards")
        with self._lock:
            target = self._now + seconds

        fired = 0
        while True:
            with self._lock:
                if not self._heap:
                    self._now = target
                    break
                entry = self._heap[0]
                if entry.cancelled:
                    heapq.heappop(self._heap)
                    continue
                if entry.due > target:
                    self._now = target
                    break
                heapq.heappop(self._heap)
                # Time is the deadline while the callback runs, so anything it
                # schedules is relative to when it actually fired.
                self._now = entry.due
            _fire(entry)
            fired += 1
        return fired

    @property
    def pending(self) -> list[str]:
        """Names of timers still waiting, for diagnosis in a failing test."""
        with self._lock:
            return [e.name for e in sorted(self._heap) if not e.cancelled]


def _fire(entry: _ScheduledCall) -> None:
    if entry.cancelled:
        return
    try:
        entry.callback()
    except Exception:  # pragma: no cover - callback is platform code
        # A failing timer must not take the scheduler thread down with it;
        # the platform would then silently stop expiring locks.
        log.exception("scheduled call %s raised", entry.name or "<unnamed>")
