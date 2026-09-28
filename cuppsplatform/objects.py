"""Workstation, user and application management (chapters 7, 8 and 9).

Three managed object types, each a state machine plus the deadlines its
chapter sets.  The deadlines are the substance: a workstation that will not
stop becomes a zombie after ``WsSpgTime``, an idle user is timed out after
``UsrToTime``, an application may defer a directed shutdown only
``MaxSpgDeferTimes`` times.  Those are what a compliance test exercises, and
what an implementation built around "it usually works" gets wrong.

Every timer runs on the injected :class:`~cuppsplatform.clock.Clock`, so the
whole lifecycle is deterministic under test rather than depending on real
600-second waits.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from cupps import params

from .clock import Clock, Timer
from .events import Event, EventBus, attach_state_machine
from .states import (
    ApplicationState,
    StateMachine,
    UserState,
    WorkstationState,
    new_machine,
)

log = logging.getLogger("cuppsplatform.objects")


class _Managed:
    """Shared timer plumbing for a managed object."""

    def __init__(
        self, machine_name: str, subject: str, *, bus: EventBus, clock: Clock,
        event_token: str = "",
    ) -> None:
        self.subject = subject
        self._bus = bus
        self._clock = clock
        self._lock = threading.RLock()
        self._timers: dict[str, Timer] = {}
        self.machine: StateMachine = new_machine(machine_name, subject=subject)
        attach_state_machine(bus, self.machine, event_token=event_token)

    @property
    def state(self) -> Enum:
        return self.machine.state

    def _arm(self, name: str, delay: float, callback: Callable[[], None]) -> None:
        """Arm a named timer, replacing any previous one of that name."""
        self._cancel(name)
        self._timers[name] = self._clock.schedule(
            params.with_tolerance(delay), callback, name=f"{self.subject}:{name}"
        )

    def _cancel(self, name: str) -> None:
        timer = self._timers.pop(name, None)
        if timer is not None:
            timer.cancel()

    def _cancel_all(self) -> None:
        for name in list(self._timers):
            self._cancel(name)

    @property
    def pending_timers(self) -> list[str]:
        """Armed timer names, for diagnosis."""
        with self._lock:
            return sorted(self._timers)


# ---------------------------------------------------------------------------
# Chapter 7 -- Workstation
# ---------------------------------------------------------------------------


@dataclass
class Alert:
    """A CUPPS Alert Message (section 6.2 definition).

    Entered by an administrator and distributed to every started workstation.
    """

    text: str
    entered_by: str
    entered_at: str = ""
    alert_id: int = 0


class ManagedWorkstation(_Managed):
    """One CUPPS workstation (chapter 7)."""

    def __init__(
        self, name: str, *, bus: EventBus, clock: Clock,
        auto_start: bool = True,
    ) -> None:
        super().__init__("workstation", name, bus=bus, clock=clock)
        self.name = name
        #: Whether the platform starts this workstation when it reaches pStd.
        self.auto_start = auto_start
        self.users: dict[str, "ManagedUser"] = {}
        self.alerts: list[Alert] = []
        self._delivered: list[int] = []

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            self.machine.enter(WorkstationState.STG, reason="workstation startup")

    def started(self) -> None:
        with self._lock:
            if self.machine.state is WorkstationState.STG:
                self.machine.enter(WorkstationState.STD, reason="workstation ready")

    def stop(self, *, reason: str = "workstation shutdown") -> None:
        """Enter wSpg, bounded by ``WsSpgTime`` (section 26.11.45)."""
        with self._lock:
            if self.machine.state in (WorkstationState.STP, WorkstationState.SPG):
                return
            self.machine.enter(WorkstationState.SPG, reason=reason)
            self._arm("WsSpgTime", params.WS_SPG_TIME, self._shutdown_expired)
        for user in list(self.users.values()):
            user.log_out(reason="workstation stopping")

    def stopped(self) -> None:
        with self._lock:
            self._cancel_all()
            if self.machine.state is WorkstationState.SPG:
                self.machine.enter(WorkstationState.STP, reason="workstation stopped")

    def _shutdown_expired(self) -> None:
        with self._lock:
            if self.machine.state is WorkstationState.SPG:
                log.warning(
                    "%s did not stop within WsSpgTime (%.0fs); moving to wZom",
                    self.name, params.WS_SPG_TIME,
                )
                self.machine.enter(
                    WorkstationState.ZOM, reason="WsSpgTime expired"
                )

    # -- users ------------------------------------------------------------

    def user_logged_in(self, user: "ManagedUser") -> None:
        """A user logged in: the workstation becomes busy."""
        with self._lock:
            self.users[user.user_name] = user
            if self.machine.can_enter(WorkstationState.BSY):
                self.machine.enter(WorkstationState.BSY, reason="user logged in")

    def user_logged_out(self, user_name: str) -> None:
        with self._lock:
            self.users.pop(user_name, None)
            if not self.users and self.machine.state is WorkstationState.BSY:
                self.machine.enter(WorkstationState.STD, reason="no users")

    # -- alerts (section 6.2.1, 7.1.1) ------------------------------------

    def deliver_alert(self, alert: Alert) -> bool:
        """Queue an alert and enter wAlt.

        Only a started workstation takes an alert: section 6.2.1 sends to
        workstations in wStd, wBsy or wAlt.  Returns whether it was accepted,
        because the platform must log a failure for any that refuses.
        """
        with self._lock:
            if self.machine.state not in (
                WorkstationState.STD, WorkstationState.BSY, WorkstationState.ALT
            ):
                return False
            self.alerts.append(alert)
            if self.machine.can_enter(WorkstationState.ALT):
                self.machine.enter(WorkstationState.ALT, reason="alert received")
            # Section 26.11.44: an undisplayed alert is discarded after
            # WsAltTime.
            self._arm(
                f"WsAltTime:{alert.alert_id}",
                params.WS_ALT_TIME,
                lambda: self._discard_alert(alert),
            )
            return True

    def acknowledge_alert(self, alert: Alert) -> None:
        """Report delivery to the end-user, leaving wAlt."""
        with self._lock:
            self._cancel(f"WsAltTime:{alert.alert_id}")
            if alert in self.alerts:
                self.alerts.remove(alert)
                self._delivered.append(alert.alert_id)
            self._leave_alert_state("alert acknowledged")

    def _discard_alert(self, alert: Alert) -> None:
        with self._lock:
            if alert in self.alerts:
                log.info(
                    "%s discarding alert %d after WsAltTime (%.0fs)",
                    self.name, alert.alert_id, params.WS_ALT_TIME,
                )
                self.alerts.remove(alert)
            self._leave_alert_state("WsAltTime expired")

    def _leave_alert_state(self, reason: str) -> None:
        if self.alerts or self.machine.state is not WorkstationState.ALT:
            return
        target = WorkstationState.BSY if self.users else WorkstationState.STD
        self.machine.enter(target, reason=reason)

    @property
    def delivered_alerts(self) -> list[int]:
        with self._lock:
            return list(self._delivered)


# ---------------------------------------------------------------------------
# Chapter 8 -- User
# ---------------------------------------------------------------------------


class TimeoutAction(str, Enum):
    """What a timed-out user does next.

    Section 8.1.6: "Depending on the system's configuration, timeout will
    result in the user changing to uSs or uSpg."
    """

    SCREEN_SAVER = "uSs"
    LOG_OUT = "uSpg"


class ManagedUser(_Managed):
    """One authenticated end-user session (chapter 8)."""

    def __init__(
        self,
        user_name: str,
        *,
        workstation: ManagedWorkstation,
        bus: EventBus,
        clock: Clock,
        timeout_action: TimeoutAction = TimeoutAction.SCREEN_SAVER,
    ) -> None:
        super().__init__("user", user_name, bus=bus, clock=clock)
        self.user_name = user_name
        self.workstation = workstation
        self.timeout_action = timeout_action
        self.applications: dict[int, "ManagedApplication"] = {}
        self._failed_logins = 0

    # -- authentication (section 26.11.40) --------------------------------

    def record_failed_login(self) -> bool:
        """Count a failed attempt; returns True once the limit is reached.

        ``UsrAthTries`` is 3 successive failures on a workstation.
        """
        with self._lock:
            self._failed_logins += 1
            return self._failed_logins >= params.USR_ATH_TRIES

    def reset_failed_logins(self) -> None:
        with self._lock:
            self._failed_logins = 0

    @property
    def failed_logins(self) -> int:
        with self._lock:
            return self._failed_logins

    # -- lifecycle --------------------------------------------------------

    def log_in(self) -> None:
        with self._lock:
            self.machine.enter(UserState.STG, reason="user login")

    def started(self) -> None:
        """Login finished; the user is in normal operation."""
        with self._lock:
            if self.machine.state is UserState.STG:
                self.machine.enter(UserState.STD, reason="login complete")
                self.reset_failed_logins()
                self._arm_idle_timer()
        self.workstation.user_logged_in(self)

    def activity(self) -> None:
        """Record end-user activity, restarting the idle timer.

        Section 26.11.43 counts keyboard input, mouse operation and device
        usage, so device I/O on this user's applications counts too.
        """
        with self._lock:
            if self.machine.state is UserState.SS:
                self.machine.enter(UserState.STD, reason="user activity")
            elif self.machine.state is UserState.TO:
                # A timed-out user returning is not a transition the table
                # allows directly; they pass through the screen saver.
                self.machine.enter(UserState.SS, reason="user activity")
                self.machine.enter(UserState.STD, reason="user activity")
            if self.machine.state is UserState.STD:
                self._arm_idle_timer()

    def _arm_idle_timer(self) -> None:
        self._arm("UsrToTime", params.USR_TO_TIME, self._idle_expired)

    def _idle_expired(self) -> None:
        """``UsrToTime`` of inactivity: the user is timed out."""
        with self._lock:
            if self.machine.state is not UserState.STD:
                return
            self.machine.enter(UserState.TO, reason="UsrToTime expired")
            if self.timeout_action is TimeoutAction.LOG_OUT:
                self._begin_log_out("timed out")
                return
            self.machine.enter(UserState.SS, reason="screen saver")
            # Section 26.11.42: the screen saver runs at most UsrSsTime
            # before the user is logged out.
            self._arm("UsrSsTime", params.USR_SS_TIME, self._screen_saver_expired)

    def _screen_saver_expired(self) -> None:
        with self._lock:
            if self.machine.state is UserState.SS:
                self._begin_log_out("UsrSsTime expired")

    def log_out(self, *, reason: str = "user logout") -> None:
        with self._lock:
            if self.machine.state in (UserState.STP, UserState.SPG):
                return
            self._begin_log_out(reason)

    def _begin_log_out(self, reason: str) -> None:
        self._cancel("UsrToTime")
        self._cancel("UsrSsTime")
        self.machine.enter(UserState.SPG, reason=reason)
        # Section 26.11.41: uSpg -> uZom once UsrSpgTime elapses.
        self._arm("UsrSpgTime", params.USR_SPG_TIME, self._logout_expired)
        for application in list(self.applications.values()):
            application.request_stop(reason="user logging out")

    def stopped(self) -> None:
        with self._lock:
            self._cancel_all()
            if self.machine.state is UserState.SPG:
                self.machine.enter(UserState.STP, reason="logout complete")
        self.workstation.user_logged_out(self.user_name)

    def _logout_expired(self) -> None:
        with self._lock:
            if self.machine.state is UserState.SPG:
                log.warning(
                    "%s did not log out within UsrSpgTime (%.0fs); moving to uZom",
                    self.user_name, params.USR_SPG_TIME,
                )
                self.machine.enter(UserState.ZOM, reason="UsrSpgTime expired")

    def add_application(self, application: "ManagedApplication") -> None:
        with self._lock:
            self.applications[application.instance_id] = application

    def remove_application(self, instance_id: int) -> None:
        with self._lock:
            self.applications.pop(instance_id, None)


# ---------------------------------------------------------------------------
# Chapter 9 -- Application
# ---------------------------------------------------------------------------


class ManagedApplication(_Managed):
    """One application or COTS instance (chapter 9)."""

    def __init__(
        self,
        instance_id: int,
        application_name: str,
        *,
        bus: EventBus,
        clock: Clock,
        user: Optional[ManagedUser] = None,
        is_cots: bool = False,
        event_token: str = "",
        #: How long an application may sit in aAth before the platform gives
        #: up on it. The specification names no parameter for this: AppAthTime
        #: and AuthReqMaxTime both bound the *platform's* processing, not the
        #: application's silence. PltSockMaxIdleTime is the closest fit, since
        #: section 26.11.33 covers "the period immediately after a socket is
        #: connected and before any character is received", so it is the
        #: default -- and it is configurable because a site may want tighter.
        authenticate_within: Optional[float] = None,
    ) -> None:
        super().__init__(
            "application", f"{application_name}#{instance_id}",
            bus=bus, clock=clock, event_token=event_token,
        )
        self.instance_id = instance_id
        self.application_name = application_name
        self.user = user
        self.is_cots = is_cots
        self.event_token = event_token
        self.authenticate_within = (
            authenticate_within
            if authenticate_within is not None
            else params.PLT_SOCK_MAX_IDLE_TIME
        )
        self._defer_count = 0
        self._stop_requested = False
        if user is not None:
            user.add_application(self)

    # -- start ------------------------------------------------------------

    def start(self) -> None:
        """Enter aStg, bounded by ``AppStgTime`` (section 26.11.3)."""
        with self._lock:
            self.machine.enter(ApplicationState.STG, reason="application launch")
            self._arm("AppStgTime", params.APP_STG_TIME, self._startup_expired)

    def _startup_expired(self) -> None:
        with self._lock:
            if self.machine.state is ApplicationState.STG:
                log.warning(
                    "%s did not start within AppStgTime (%.0fs); moving to aZom",
                    self.subject, params.APP_STG_TIME,
                )
                self.machine.enter(
                    ApplicationState.ZOM, reason="AppStgTime expired"
                )

    def started(self) -> None:
        """Startup finished.

        A COTS application goes straight to aCts; a CUPPS application goes to
        aAth and must then authenticate (Table 9.2).
        """
        with self._lock:
            self._cancel("AppStgTime")
            if self.machine.state is not ApplicationState.STG:
                return
            if self.is_cots:
                self.machine.enter(ApplicationState.CTS, reason="COTS application")
                return
            self.machine.enter(ApplicationState.ATH, reason="awaiting authentication")
            self._arm(
                "authenticateWithin",
                self.authenticate_within,
                self._authentication_expired,
            )

    def authenticated(self, *, event_token: str = "") -> None:
        """The application authenticated; it enters normal operation."""
        with self._lock:
            self._cancel("authenticateWithin")
            if event_token:
                self.event_token = event_token
            if self.machine.state is ApplicationState.ATH:
                self.machine.enter(ApplicationState.STD, reason="authenticated")

    def _authentication_expired(self) -> None:
        """Section 9.1.1: an application that does not authenticate is stopped."""
        with self._lock:
            if self.machine.state is not ApplicationState.ATH:
                return
            log.info(
                "%s did not authenticate within %.0fs; directing it to stop",
                self.subject, self.authenticate_within,
            )
            self._begin_stop("authentication timeout", directed=True)

    # -- stop (section 29.2) ----------------------------------------------

    @property
    def defer_count(self) -> int:
        with self._lock:
            return self._defer_count

    @property
    def may_defer(self) -> bool:
        """Whether another deferral is within ``MaxSpgDeferTimes``."""
        with self._lock:
            return self._defer_count < params.MAX_SPG_DEFER_TIMES

    def request_stop(self, *, reason: str = "requested") -> None:
        """Ask the application to stop gracefully (section 29.2)."""
        with self._lock:
            if self.machine.state in (
                ApplicationState.SPG, ApplicationState.STP, ApplicationState.ZOM
            ):
                return
            self._stop_requested = True
            self._begin_stop(reason, directed=False)

    def defer_stop(self) -> bool:
        """Record a deferral; returns whether it was allowed.

        Section 29.2 permits ``MaxSpgDeferTimes`` deferrals, typically to
        finish a host or device transaction.  Beyond that the application is
        directed to stop.
        """
        with self._lock:
            if self.machine.state is not ApplicationState.SPG:
                return False
            if self._defer_count >= params.MAX_SPG_DEFER_TIMES:
                return False
            self._defer_count += 1
            # Each deferral buys another AppSpgTime.
            self._arm("AppSpgTime", params.APP_SPG_TIME, self._stop_expired)
            return True

    def _begin_stop(self, reason: str, *, directed: bool) -> None:
        self._cancel("authenticateWithin")
        self._cancel("AppStgTime")
        self.machine.enter(ApplicationState.SPG, reason=reason)
        # Section 26.11.2: AppSpgTime to shut down once directed.
        self._arm("AppSpgTime", params.APP_SPG_TIME, self._stop_expired)
        if directed:
            self._bus.raise_event(
                Event(
                    name="applicationInstBlockEvent",
                    subject=self.subject,
                    event_token=self.event_token,
                    attributes={"reason": reason},
                )
            )

    def _stop_expired(self) -> None:
        with self._lock:
            if self.machine.state is ApplicationState.SPG:
                log.warning(
                    "%s did not stop within AppSpgTime (%.0fs) after %d "
                    "deferral(s); moving to aZom",
                    self.subject, params.APP_SPG_TIME, self._defer_count,
                )
                self.machine.enter(
                    ApplicationState.ZOM, reason="AppSpgTime expired"
                )

    def stopped(self) -> None:
        """The application terminated; the platform cleans up.

        ``AppStpTime`` bounds the platform's own clean-up (section 26.11.4).
        """
        with self._lock:
            self._cancel_all()
            if self.machine.state in (ApplicationState.SPG, ApplicationState.ZOM):
                self.machine.enter(ApplicationState.STP, reason="application stopped")
        if self.user is not None:
            self.user.remove_application(self.instance_id)

    def failed(self, reason: str) -> None:
        """The application crashed or stopped responding."""
        with self._lock:
            self._cancel_all()
            if self.machine.can_enter(ApplicationState.ZOM):
                self.machine.enter(ApplicationState.ZOM, reason=reason)
