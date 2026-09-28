"""Device management (TS 01.04.0004 chapter 10 and section 26.13).

A managed device is a device state machine plus the locking rules, and the
locking rules are where platforms most often differ from the specification.
The two that matter:

**Two lock methods, never mixed.** ``byConnection`` holds a lock for one
connection between its lock and unlock.  ``byDeviceToken`` holds it for every
connection sharing a device token, from the first lock to the last unlock --
intended for threads within one application, not between applications
(section 26.13.1).  A request in the other method while a lock is held is
refused with ``switchingLockMethodsNotAllowed``.

**Expiry is not the same as release.** A lock that sees no device I/O for
``DevLkdTime`` expires, and the platform notifies *every* application holding
the device with ``<deviceLockExpiredEvent>``.  An explicit unlock or release
notifies nobody sharing the same token, because section 26.13.2 says the
platform "must assume that all sessions using the same token already know
about this action".  Getting that backwards produces a platform that either
floods applications with events or leaves them believing they still hold a
lock they have lost.

The AEA asymmetry from the Table 10.1 footnote is implemented here too: a
device whose interface mode is AEA moves straight to ``dLkd`` and stays there
while in use, rather than moving to ``dBsy``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from cupps import params
from cupps.results import InterfaceMode, LockMethod

from .clock import Clock, Timer
from .events import Event, EventBus, attach_state_machine
from .states import DeviceState, StateMachine, new_machine

log = logging.getLogger("cuppsplatform.device")

#: Device types that are macro devices: locking one implicitly locks its
#: sub-devices (section 26.13.3).
MACRO_TYPES = frozenset({"BG"})

#: Special Mode devices do not support locking at all; an attempt is an
#: illogical message (section 30.20 note).
SPECIAL_MODE_TYPES = frozenset({"ZL", "ZI"})


class LockRefused(Exception):
    """A lock request the platform must refuse, carrying the result code."""

    def __init__(self, result: str, locker: Optional[dict] = None) -> None:
        super().__init__(result)
        self.result = result
        self.locker = locker or {}


class LockingNotSupported(Exception):
    """Locking was attempted on a Special Mode device (section 30.20)."""


@dataclass
class LockHolder:
    """Who holds a lock, reported in ``<deviceLockerInfo>`` on refusal."""

    method: LockMethod
    #: Connection identity for byConnection, device token for byDeviceToken.
    owner: str
    airline: str = ""
    application_name: str = ""
    application_version: str = ""

    def info(self) -> dict[str, str]:
        """The attributes section 30.7 requires on a refusal."""
        return {
            key: value
            for key, value in (
                ("airline", self.airline),
                ("applicationName", self.application_name),
                ("applicationVersion", self.application_version),
                ("lockMethod", self.method.value),
            )
            if value
        }


@dataclass
class AcquiredSession:
    """One application session that has acquired this device."""

    session_id: int
    device_token: str
    airline_id: str
    interface_mode: InterfaceMode
    notify: Callable[[Event], None] = field(repr=False)
    application_name: str = ""


class ManagedDevice:
    """One CUPPS managed device, with its state machine and lock."""

    def __init__(
        self,
        name: str,
        device_type: str,
        *,
        bus: EventBus,
        clock: Clock,
        parent: Optional["ManagedDevice"] = None,
    ) -> None:
        self.name = name
        self.device_type = device_type.upper()
        self.parent = parent
        self.sub_devices: list["ManagedDevice"] = []

        self._bus = bus
        self._clock = clock
        self._lock = threading.RLock()

        self.machine: StateMachine = new_machine("device", subject=name)
        attach_state_machine(bus, self.machine)

        self._sessions: dict[int, AcquiredSession] = {}
        self._holder: Optional[LockHolder] = None
        #: byDeviceToken locks are shared, so the holders are counted.
        self._token_lock_sessions: set[int] = set()
        self._lock_timer: Optional[Timer] = None
        self._lifecycle_timer: Optional[Timer] = None
        #: True while the device is implicitly locked by its parent macro
        #: device (section 26.13.3).
        self._implicitly_locked = False

    # -- shape ------------------------------------------------------------

    @property
    def is_macro(self) -> bool:
        return self.device_type in MACRO_TYPES

    @property
    def supports_locking(self) -> bool:
        return self.device_type not in SPECIAL_MODE_TYPES

    @property
    def state(self) -> DeviceState:
        return self.machine.state

    @property
    def locked(self) -> bool:
        with self._lock:
            return self._holder is not None or self._implicitly_locked

    @property
    def holder(self) -> Optional[LockHolder]:
        with self._lock:
            return self._holder

    def add_sub_device(self, device: "ManagedDevice") -> "ManagedDevice":
        device.parent = self
        self.sub_devices.append(device)
        return device

    def walk(self) -> Iterable["ManagedDevice"]:
        yield self
        for sub in self.sub_devices:
            yield from sub.walk()

    # -- lifecycle (chapter 10) -------------------------------------------

    def start(self) -> None:
        """Take the device from dStp through dStg to dStd.

        ``DevStgTime`` bounds the startup: a device that has not finished
        starting by then is moved to dZom (section 26.11.14).
        """
        with self._lock:
            self.machine.enter(DeviceState.STG, reason="device startup")
            self._arm_lifecycle(
                params.DEV_STG_TIME,
                self._startup_expired,
                "DevStgTime",
            )
        for sub in self.sub_devices:
            sub.start()

    def started(self) -> None:
        """Report that initialisation finished; the device becomes usable."""
        with self._lock:
            self._cancel_lifecycle()
            if self.machine.state is DeviceState.STG:
                self.machine.enter(DeviceState.STD, reason="device ready")

    def _startup_expired(self) -> None:
        with self._lock:
            if self.machine.state is DeviceState.STG:
                log.warning(
                    "%s did not start within DevStgTime (%.0fs); moving to dZom",
                    self.name, params.DEV_STG_TIME,
                )
                self.machine.enter(DeviceState.ZOM, reason="DevStgTime expired")

    def stop(self) -> None:
        """Take the device to dSpg, bounded by ``DevSpgTime``."""
        with self._lock:
            if self.machine.state in (DeviceState.STP, DeviceState.SPG):
                return
            pending = self._release_lock_internal(
                notify=True, reason="device stopping"
            )
            self.machine.enter(DeviceState.SPG, reason="device shutdown")
            self._arm_lifecycle(
                params.DEV_SPG_TIME, self._shutdown_expired, "DevSpgTime"
            )
        self._deliver(pending)
        for sub in self.sub_devices:
            sub.stop()

    def stopped(self) -> None:
        with self._lock:
            self._cancel_lifecycle()
            if self.machine.state is DeviceState.SPG:
                self.machine.enter(DeviceState.STP, reason="device stopped")

    def _shutdown_expired(self) -> None:
        with self._lock:
            if self.machine.state is DeviceState.SPG:
                log.warning(
                    "%s did not stop within DevSpgTime (%.0fs); moving to dZom",
                    self.name, params.DEV_SPG_TIME,
                )
                self.machine.enter(DeviceState.ZOM, reason="DevSpgTime expired")

    def fault(self, reason: str) -> None:
        """Move the device to dErr, e.g. paper out or a malfunction."""
        with self._lock:
            if self.machine.can_enter(DeviceState.ERR):
                self.machine.enter(DeviceState.ERR, reason=reason)

    def cleared(self) -> None:
        """Recover from dErr back to whatever the lock state implies."""
        with self._lock:
            if self.machine.state is not DeviceState.ERR:
                return
            target = DeviceState.LKD if self.locked else DeviceState.STD
            self.machine.enter(target, reason="fault cleared")

    def _arm_lifecycle(self, delay: float, callback, name: str) -> None:
        self._cancel_lifecycle()
        self._lifecycle_timer = self._clock.schedule(
            params.with_tolerance(delay), callback, name=f"{self.name}:{name}"
        )

    def _cancel_lifecycle(self) -> None:
        if self._lifecycle_timer is not None:
            self._lifecycle_timer.cancel()
            self._lifecycle_timer = None

    # -- acquisition ------------------------------------------------------

    def acquire(self, session: AcquiredSession) -> None:
        """Register a session that has acquired this device."""
        with self._lock:
            self._sessions[session.session_id] = session

    def release(self, session_id: int) -> None:
        """Deregister a session, dropping any lock it held.

        Section 26.13.2: an explicit release does not notify sessions sharing
        the token, because they are assumed to know.
        """
        with self._lock:
            self._sessions.pop(session_id, None)
            self._token_lock_sessions.discard(session_id)
            holder = self._holder
            if holder is None:
                return
            if holder.method is LockMethod.BY_CONNECTION:
                if holder.owner == str(session_id):
                    self._release_lock_internal(notify=False, reason="released")
            elif not self._token_lock_sessions:
                self._release_lock_internal(notify=False, reason="released")

    @property
    def acquired_by(self) -> list[AcquiredSession]:
        with self._lock:
            return list(self._sessions.values())

    # -- locking (section 26.13) ------------------------------------------

    def lock(
        self,
        session: AcquiredSession,
        method: LockMethod = LockMethod.BY_CONNECTION,
    ) -> str:
        """Lock the device, returning the result code for the response.

        Returns ``OK`` or ``OK-deviceAlreadyLocked``; raises
        :class:`LockRefused` with ``deviceLocked`` or
        ``switchingLockMethodsNotAllowed`` otherwise.
        """
        if not self.supports_locking:
            raise LockingNotSupported(
                f"{self.name} is a Special Mode device; locking one is an "
                f"illogical message (section 30.20)"
            )

        with self._lock:
            # A sub-device of a macro device that someone else holds cannot
            # be locked independently (section 26.13.3).
            blocking_parent = self._blocking_parent(session)
            if blocking_parent is not None:
                raise LockRefused(
                    "switchingLockMethodsNotAllowed"
                    if blocking_parent.holder
                    and blocking_parent.holder.method is not method
                    else "deviceLocked",
                    blocking_parent.holder.info() if blocking_parent.holder else {},
                )

            owner = (
                str(session.session_id)
                if method is LockMethod.BY_CONNECTION
                else session.device_token
            )
            holder = self._holder

            if holder is None:
                self._holder = LockHolder(
                    method=method,
                    owner=owner,
                    airline=session.airline_id,
                    application_name=session.application_name,
                )
                if method is LockMethod.BY_DEVICE_TOKEN:
                    self._token_lock_sessions.add(session.session_id)
                self._enter_locked_state(session)
                self._arm_lock_timer()
                self._lock_sub_devices()
                return "OK"

            if holder.method is not method:
                # Section 30.7: byConnection and byDeviceToken never mix.
                raise LockRefused("switchingLockMethodsNotAllowed", holder.info())

            if holder.owner != owner:
                raise LockRefused("deviceLocked", holder.info())

            # Same owner, same method: a renewal, which resets DevLkdTime.
            if method is LockMethod.BY_DEVICE_TOKEN:
                already = session.session_id in self._token_lock_sessions
                self._token_lock_sessions.add(session.session_id)
                self._arm_lock_timer()
                return "OK-deviceAlreadyLocked" if already else "OK"
            self._arm_lock_timer()
            return "OK-deviceAlreadyLocked"

    def unlock(self, session: AcquiredSession) -> None:
        """Release this session's claim on the lock."""
        if not self.supports_locking:
            raise LockingNotSupported(
                f"{self.name} is a Special Mode device and holds no lock"
            )
        with self._lock:
            holder = self._holder
            if holder is None:
                return
            if holder.method is LockMethod.BY_DEVICE_TOKEN:
                self._token_lock_sessions.discard(session.session_id)
                if self._token_lock_sessions:
                    # Other sessions on the same token still hold it.
                    return
            self._release_lock_internal(notify=False, reason="unlocked")

    def touch(self) -> None:
        """Record device I/O, which restarts the ``DevLkdTime`` timer."""
        with self._lock:
            if self._holder is not None:
                self._arm_lock_timer()

    def begin_operation(self) -> None:
        """Mark the device busy (dBsy), if its mode uses that state."""
        with self._lock:
            self.touch()
            if self.machine.state is DeviceState.LKD and not self._is_aea():
                self.machine.enter(DeviceState.BSY, reason="device in use")

    def end_operation(self) -> None:
        with self._lock:
            self.touch()
            if self.machine.state is DeviceState.BSY:
                self.machine.enter(DeviceState.LKD, reason="operation complete")

    # -- lock internals ---------------------------------------------------

    def _is_aea(self) -> bool:
        """True when any acquiring session is in AEA mode.

        Table 10.1 footnote: in AEA mode the device goes straight to dLkd and
        stays there while in use, rather than moving to dBsy.
        """
        return any(
            session.interface_mode is InterfaceMode.AEA
            for session in self._sessions.values()
        )

    def _enter_locked_state(self, session: AcquiredSession) -> None:
        if self.machine.can_enter(DeviceState.LKD):
            self.machine.enter(DeviceState.LKD, reason="locked")

    def _blocking_parent(
        self, session: AcquiredSession
    ) -> Optional["ManagedDevice"]:
        """A macro ancestor locked by someone other than this session."""
        parent = self.parent
        while parent is not None:
            holder = parent.holder
            if holder is not None:
                owned = holder.owner in (
                    str(session.session_id), session.device_token
                )
                if not owned:
                    return parent
            parent = parent.parent
        return None

    def _lock_sub_devices(self) -> None:
        """A macro device's lock covers its sub-devices (section 26.13.3)."""
        if not self.is_macro:
            return
        for sub in self.sub_devices:
            sub._implicitly_locked = True
            if sub.machine.can_enter(DeviceState.LKD):
                sub.machine.enter(DeviceState.LKD, reason="macro device locked")

    def _unlock_sub_devices(self) -> None:
        """Release implicit locks, keeping any the sub-device holds itself."""
        if not self.is_macro:
            return
        for sub in self.sub_devices:
            sub._implicitly_locked = False
            if sub.holder is None and sub.machine.state is DeviceState.LKD:
                sub.machine.enter(DeviceState.STD, reason="macro device unlocked")

    def _arm_lock_timer(self) -> None:
        if self._lock_timer is not None:
            self._lock_timer.cancel()
        self._lock_timer = self._clock.schedule(
            params.with_tolerance(params.DEV_LKD_TIME),
            self._lock_expired,
            name=f"{self.name}:DevLkdTime",
        )

    def _lock_expired(self) -> None:
        """``DevLkdTime`` elapsed with no I/O: the lock is lost.

        Section 26.13.2 requires every application holding the device to be
        told, including the one that held the lock.
        """
        with self._lock:
            if self._holder is None:
                return
            log.info(
                "%s lock expired after DevLkdTime (%.0fs)",
                self.name, params.DEV_LKD_TIME,
            )
            pending = self._release_lock_internal(
                notify=True, reason="DevLkdTime expired"
            )
        self._deliver(pending)

    def _release_lock_internal(
        self, *, notify: bool, reason: str
    ) -> Optional[tuple[Event, list[AcquiredSession]]]:
        """Drop the lock and return the delivery the caller must make.

        This only mutates state. Delivery is deliberately *not* done here:
        it is returned so the caller can perform it after releasing the
        device's mutex. See :meth:`_deliver`.
        """
        holder, self._holder = self._holder, None
        self._token_lock_sessions.clear()
        if self._lock_timer is not None:
            self._lock_timer.cancel()
            self._lock_timer = None
        if holder is None:
            return None

        self._unlock_sub_devices()
        if self.machine.state in (DeviceState.LKD, DeviceState.BSY):
            self.machine.enter(DeviceState.STD, reason=reason)

        if not notify:
            return None
        # Every application that has the device acquired is told, not only
        # the lock's owner (section 26.13.2).
        event = Event(
            name="deviceLockExpiredEvent",
            subject=self.name,
            attributes={"reason": reason},
        )
        return event, list(self._sessions.values())

    def _deliver(
        self, pending: Optional[tuple[Event, list[AcquiredSession]]]
    ) -> None:
        """Publish a pending notification, with the device mutex released.

        An application whose socket has stopped draining will block here.
        Holding the device mutex across that would let one wedged client
        freeze a shared device for every other application, which in an
        airport means one stuck gate position taking out a boarding pass
        printer.
        """
        if pending is None:
            return
        event, sessions = pending
        self._bus.raise_event(event)
        for session in sessions:
            try:
                session.notify(event)
            except Exception:  # pragma: no cover - session is caller code
                log.exception("notifying %s of lock expiry failed", self.name)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ManagedDevice {self.name} {self.device_type} {self.state.value}>"
