"""The five CUPPS state machines (TS 01.04.0004, Part II).

A CUPPS platform is defined by five interacting state machines (Table 6.1):

===========  ========  =============================================
Machine      Chapter   Manages
===========  ========  =============================================
Platform     6         the whole platform, and its workstations
Workstation  7         one workstation's runtime environment
User         8         one authenticated end-user's session
Application  9         one application or COTS instance
Device       10        one CUPPS managed device (not sub-devices)
===========  ========  =============================================

Each chapter publishes a state summary and a transition table, and this module
encodes those tables verbatim.  They are not guidance: a transition the table
marks impossible is a defect, and compliance testing exercises them.

Every state entry fires ``<xxxEnteredEvent>`` and every exit fires
``<xxxExitedEvent>`` *before* the next state is entered (see for example
section 6.2.1, "When a Platform exits the pAlt state and before it enters its
next state, it fires <pAltExitedEvent>").  That ordering is what a listener
relies on to see a coherent sequence, so it is enforced here rather than left
to each call site.

Section 10.3 also requires platforms to write a log entry for entry to and
exit from every device state; the machine emits both transitions so a logger
attached to the bus satisfies that without special-casing.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Iterable, Optional

log = logging.getLogger("cuppsplatform.states")


class IllegalTransition(RuntimeError):
    """A transition the specification's table marks as impossible.

    Raised rather than logged: a platform that can reach an undefined state
    has a defect that compliance testing will find, and finding it in
    development is cheaper.
    """

    def __init__(self, machine: str, current: str, target: str) -> None:
        super().__init__(
            f"{machine}: {current} -> {target} is not an allowed transition "
            f"(see the state transition table in the specification)"
        )
        self.machine = machine
        self.current = current
        self.target = target


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------


class PlatformState(str, Enum):
    """Table 6.2. ``pStp`` is the default state."""

    ALT = "pAlt"   # a CUPPS Alert Message is being distributed
    STD = "pStd"   # started; the normal operating state
    STG = "pStg"   # starting; bootstrap in progress
    STP = "pStp"   # stopped; the default state
    SPG = "pSpg"   # stopping


class WorkstationState(str, Enum):
    """Table 7.1. ``wStp`` is the default state."""

    ALT = "wAlt"   # an alert has been distributed to this workstation
    BSY = "wBsy"   # running with an end-user logged in
    STD = "wStd"   # started; the normal operating state
    STG = "wStg"   # starting
    STP = "wStp"   # stopped; the default state
    SPG = "wSpg"   # stopping
    ZOM = "wZom"   # failed


class UserState(str, Enum):
    """Table 8.1. ``uStp`` is the default state."""

    SPG = "uSpg"   # logging out
    STD = "uStd"   # started; the normal operating state
    STP = "uStp"   # not logged in; the default state
    STG = "uStg"   # starting
    SS = "uSs"     # screen saver displayed
    TO = "uTo"     # timed out after UsrToTime
    ZOM = "uZom"   # failed


class ApplicationState(str, Enum):
    """Table 9.1. ``aStp`` is the default state."""

    ATH = "aAth"   # started, awaiting authentication
    CTS = "aCts"   # a COTS application is running
    SPG = "aSpg"   # stopping
    STD = "aStd"   # started and authenticated
    STG = "aStg"   # starting
    STP = "aStp"   # stopped; the default state
    ZOM = "aZom"   # failed


class DeviceState(str, Enum):
    """Table 10.1. ``dStp`` is the default state."""

    BSY = "dBsy"   # locked and operating
    ERR = "dErr"   # malfunction, paper out, paper jam or similar
    LKD = "dLkd"   # locked by an application for exclusive access
    SPG = "dSpg"   # stopping
    STD = "dStd"   # started
    STG = "dStg"   # starting
    STP = "dStp"   # stopped; the default state
    ZOM = "dZom"   # failed


# ---------------------------------------------------------------------------
# Transition tables, transcribed from the specification
# ---------------------------------------------------------------------------

#: Table 6.3, Platform State Transitions.
PLATFORM_TRANSITIONS: dict[PlatformState, frozenset[PlatformState]] = {
    PlatformState.STP: frozenset({PlatformState.STG}),
    PlatformState.STG: frozenset({PlatformState.STD, PlatformState.SPG}),
    PlatformState.STD: frozenset({PlatformState.ALT, PlatformState.SPG}),
    PlatformState.ALT: frozenset({PlatformState.STD, PlatformState.SPG}),
    PlatformState.SPG: frozenset({PlatformState.STP}),
}

#: Table 7.2, Workstation State Transitions.
WORKSTATION_TRANSITIONS: dict[WorkstationState, frozenset[WorkstationState]] = {
    WorkstationState.STP: frozenset({WorkstationState.STG}),
    WorkstationState.STG: frozenset(
        {WorkstationState.STD, WorkstationState.SPG, WorkstationState.ZOM}
    ),
    WorkstationState.STD: frozenset(
        {WorkstationState.BSY, WorkstationState.ALT, WorkstationState.SPG,
         WorkstationState.ZOM}
    ),
    WorkstationState.BSY: frozenset(
        {WorkstationState.STD, WorkstationState.ALT, WorkstationState.SPG,
         WorkstationState.ZOM}
    ),
    WorkstationState.ALT: frozenset(
        {WorkstationState.STD, WorkstationState.BSY, WorkstationState.SPG,
         WorkstationState.ZOM}
    ),
    WorkstationState.SPG: frozenset({WorkstationState.STP, WorkstationState.ZOM}),
    WorkstationState.ZOM: frozenset({WorkstationState.STP}),
}

#: Table 8.2, User State Transitions.
USER_TRANSITIONS: dict[UserState, frozenset[UserState]] = {
    UserState.STP: frozenset({UserState.STG}),
    UserState.STG: frozenset({UserState.STD, UserState.SPG, UserState.ZOM}),
    UserState.STD: frozenset(
        {UserState.TO, UserState.SS, UserState.SPG, UserState.ZOM}
    ),
    UserState.TO: frozenset({UserState.SS, UserState.SPG, UserState.ZOM}),
    UserState.SS: frozenset({UserState.STD, UserState.SPG, UserState.ZOM}),
    UserState.SPG: frozenset({UserState.STP, UserState.ZOM}),
    UserState.ZOM: frozenset({UserState.STP}),
}

#: Table 9.2, Application State Transitions.
APPLICATION_TRANSITIONS: dict[ApplicationState, frozenset[ApplicationState]] = {
    ApplicationState.STP: frozenset({ApplicationState.STG}),
    ApplicationState.STG: frozenset(
        {ApplicationState.CTS, ApplicationState.ATH, ApplicationState.SPG,
         ApplicationState.ZOM}
    ),
    ApplicationState.CTS: frozenset({ApplicationState.SPG, ApplicationState.ZOM}),
    ApplicationState.ATH: frozenset(
        {ApplicationState.STD, ApplicationState.SPG, ApplicationState.ZOM}
    ),
    ApplicationState.STD: frozenset({ApplicationState.SPG, ApplicationState.ZOM}),
    ApplicationState.SPG: frozenset({ApplicationState.STP, ApplicationState.ZOM}),
    ApplicationState.ZOM: frozenset({ApplicationState.STP}),
}

#: Table 10.2, Device State Transitions.
DEVICE_TRANSITIONS: dict[DeviceState, frozenset[DeviceState]] = {
    DeviceState.STP: frozenset({DeviceState.STG}),
    DeviceState.STG: frozenset(
        {DeviceState.STD, DeviceState.SPG, DeviceState.ZOM}
    ),
    DeviceState.STD: frozenset(
        {DeviceState.LKD, DeviceState.ERR, DeviceState.SPG, DeviceState.ZOM}
    ),
    DeviceState.LKD: frozenset(
        {DeviceState.STD, DeviceState.BSY, DeviceState.ERR, DeviceState.SPG,
         DeviceState.ZOM}
    ),
    DeviceState.BSY: frozenset(
        {DeviceState.LKD, DeviceState.ERR, DeviceState.SPG, DeviceState.ZOM}
    ),
    DeviceState.ERR: frozenset(
        {DeviceState.STD, DeviceState.LKD, DeviceState.BSY, DeviceState.SPG,
         DeviceState.ZOM}
    ),
    DeviceState.SPG: frozenset({DeviceState.STP, DeviceState.ZOM}),
    DeviceState.ZOM: frozenset({DeviceState.STP}),
}


# ---------------------------------------------------------------------------
# The machine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StateChange:
    """One observed entry or exit."""

    machine: str
    #: The object this machine governs: a workstation name, a device name, an
    #: application instance id. Empty for the platform itself.
    subject: str
    state: str
    #: ``entered`` or ``exited``.
    kind: str
    at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        )
    )
    reason: str = ""

    @property
    def event_name(self) -> str:
        """The event the specification names for this change.

        Section 6.2 onward name them ``<pAltEnteredEvent>``,
        ``<pAltExitedEvent>`` and so on for every state of every machine.
        """
        return f"{self.state}{'Entered' if self.kind == 'entered' else 'Exited'}Event"

    def __str__(self) -> str:
        subject = f" {self.subject}" if self.subject else ""
        reason = f" ({self.reason})" if self.reason else ""
        return f"{self.machine}{subject} {self.kind} {self.state}{reason}"


Listener = Callable[[StateChange], None]


@dataclass(frozen=True)
class MachineDefinition:
    """One state machine's shape, taken from its chapter."""

    name: str
    states: type[Enum]
    transitions: dict
    initial: Enum
    #: The chapter and table the transitions came from, for the record.
    reference: str

    def allowed(self, current: Enum) -> frozenset:
        return self.transitions.get(current, frozenset())


PLATFORM = MachineDefinition(
    name="platform", states=PlatformState, transitions=PLATFORM_TRANSITIONS,
    initial=PlatformState.STP, reference="Table 6.3",
)
WORKSTATION = MachineDefinition(
    name="workstation", states=WorkstationState,
    transitions=WORKSTATION_TRANSITIONS, initial=WorkstationState.STP,
    reference="Table 7.2",
)
USER = MachineDefinition(
    name="user", states=UserState, transitions=USER_TRANSITIONS,
    initial=UserState.STP, reference="Table 8.2",
)
APPLICATION = MachineDefinition(
    name="application", states=ApplicationState,
    transitions=APPLICATION_TRANSITIONS, initial=ApplicationState.STP,
    reference="Table 9.2",
)
DEVICE = MachineDefinition(
    name="device", states=DeviceState, transitions=DEVICE_TRANSITIONS,
    initial=DeviceState.STP, reference="Table 10.2",
)

#: Every machine, by name.
MACHINES = {
    machine.name: machine
    for machine in (PLATFORM, WORKSTATION, USER, APPLICATION, DEVICE)
}


class StateMachine:
    """One running instance of a machine definition."""

    def __init__(
        self,
        definition: MachineDefinition,
        *,
        subject: str = "",
        listeners: Optional[Iterable[Listener]] = None,
    ) -> None:
        self.definition = definition
        self.subject = subject
        self._state: Enum = definition.initial
        self._lock = threading.RLock()
        self._listeners: list[Listener] = list(listeners or [])
        self._history: list[StateChange] = []

    # -- inspection -------------------------------------------------------

    @property
    def state(self) -> Enum:
        with self._lock:
            return self._state

    @property
    def name(self) -> str:
        return self.definition.name

    def is_in(self, *states: Enum) -> bool:
        return self.state in states

    def can_enter(self, target: Enum) -> bool:
        with self._lock:
            return target in self.definition.allowed(self._state)

    @property
    def allowed_next(self) -> frozenset:
        with self._lock:
            return self.definition.allowed(self._state)

    def history(self, limit: int = 100) -> list[StateChange]:
        with self._lock:
            return list(self._history[-limit:])

    # -- listeners --------------------------------------------------------

    def add_listener(self, listener: Listener) -> None:
        """Register a callback for every entry and exit.

        Listeners run inside the transition, so the ordering the
        specification requires -- exit fired before the next state is entered
        -- is what a listener actually observes.  They must not block.
        """
        self._listeners.append(listener)

    def _emit(self, change: StateChange) -> None:
        self._history.append(change)
        if len(self._history) > 500:
            del self._history[: len(self._history) - 500]
        for listener in list(self._listeners):
            try:
                listener(change)
            except Exception:  # pragma: no cover - listener is caller's code
                log.exception("%s listener raised on %s", self.name, change)

    # -- transition -------------------------------------------------------

    def enter(self, target: Enum, *, reason: str = "") -> StateChange:
        """Move to ``target``, firing the exit then the entry event.

        Raises :class:`IllegalTransition` when the specification's table does
        not permit the move.
        """
        with self._lock:
            current = self._state
            if target not in self.definition.allowed(current):
                raise IllegalTransition(
                    self.definition.name, current.value, target.value
                )
            # Section 6.2.1 and its siblings: the exit event is fired before
            # the next state is entered.
            self._emit(
                StateChange(
                    machine=self.definition.name, subject=self.subject,
                    state=current.value, kind="exited", reason=reason,
                )
            )
            self._state = target
            change = StateChange(
                machine=self.definition.name, subject=self.subject,
                state=target.value, kind="entered", reason=reason,
            )
            self._emit(change)
            return change

    def force(self, target: Enum, *, reason: str) -> StateChange:
        """Move to ``target`` even if the table forbids it.

        The only legitimate use is recovering a machine whose real-world
        object has gone away underneath it -- a workstation that vanished, a
        device unplugged mid-transition. It is logged at warning level
        because a platform that needs this routinely has a bug.
        """
        with self._lock:
            current = self._state
            if target in self.definition.allowed(current):
                return self.enter(target, reason=reason)
            log.warning(
                "%s %s: forcing %s -> %s (%s); this transition is not in %s",
                self.definition.name, self.subject or "-", current.value,
                target.value, reason, self.definition.reference,
            )
            self._emit(
                StateChange(
                    machine=self.definition.name, subject=self.subject,
                    state=current.value, kind="exited",
                    reason=f"forced: {reason}",
                )
            )
            self._state = target
            change = StateChange(
                machine=self.definition.name, subject=self.subject,
                state=target.value, kind="entered",
                reason=f"forced: {reason}",
            )
            self._emit(change)
            return change

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        subject = f" {self.subject}" if self.subject else ""
        return f"<{self.definition.name}{subject} {self._state.value}>"


def new_machine(
    machine: str, *, subject: str = "", listeners: Optional[Iterable[Listener]] = None
) -> StateMachine:
    """Create a machine by name: platform, workstation, user, application, device."""
    try:
        definition = MACHINES[machine]
    except KeyError:
        raise KeyError(
            f"no such state machine {machine!r}; CUPPS defines "
            f"{sorted(MACHINES)}"
        ) from None
    return StateMachine(definition, subject=subject, listeners=listeners)
