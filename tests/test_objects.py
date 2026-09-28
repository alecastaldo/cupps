"""Workstation, user and application management (chapters 7, 8 and 9).

Every deadline in these chapters is asserted at its boundary: the state must
not change just before the parameter elapses and must change just after.
A timer armed with the wrong value passes a "does it eventually fire" test
and fails these.
"""

from __future__ import annotations

import pytest

from cupps import params

from cuppsplatform.clock import ManualClock
from cuppsplatform.events import EventBus
from cuppsplatform.objects import (
    Alert,
    ManagedApplication,
    ManagedUser,
    ManagedWorkstation,
    TimeoutAction,
)
from cuppsplatform.states import ApplicationState, UserState, WorkstationState

#: Just inside and just outside a deadline, within the +/-1% of section 26.11.
BEFORE = 0.98
AFTER = 1.02


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def clock():
    return ManualClock()


@pytest.fixture
def workstation(bus, clock):
    ws = ManagedWorkstation("LHRT4LB00302", bus=bus, clock=clock)
    ws.start()
    ws.started()
    return ws


def make_user(workstation, bus, clock, **kwargs):
    user = ManagedUser(
        "BA-USER", workstation=workstation, bus=bus, clock=clock, **kwargs
    )
    user.log_in()
    user.started()
    return user


# -- Chapter 7, workstation -----------------------------------------------


def test_workstation_starts_and_stops(bus, clock):
    ws = ManagedWorkstation("WS1", bus=bus, clock=clock)
    assert ws.state is WorkstationState.STP
    ws.start()
    assert ws.state is WorkstationState.STG
    ws.started()
    assert ws.state is WorkstationState.STD
    ws.stop()
    assert ws.state is WorkstationState.SPG
    ws.stopped()
    assert ws.state is WorkstationState.STP


def test_workstation_that_will_not_stop_becomes_a_zombie(workstation, clock):
    """Section 26.11.45: wSpg -> wZom after WsSpgTime."""
    workstation.stop()
    clock.advance(params.WS_SPG_TIME * BEFORE)
    assert workstation.state is WorkstationState.SPG
    clock.advance(params.WS_SPG_TIME * (AFTER - BEFORE) + 0.1)
    assert workstation.state is WorkstationState.ZOM


def test_workstation_becomes_busy_while_a_user_is_logged_in(
    workstation, bus, clock
):
    user = make_user(workstation, bus, clock)
    assert workstation.state is WorkstationState.BSY
    user.log_out()
    user.stopped()
    assert workstation.state is WorkstationState.STD


def test_alert_moves_the_workstation_to_walt(workstation):
    alert = Alert(text="Terminal 4 evacuation drill", entered_by="admin",
                  alert_id=1)
    assert workstation.deliver_alert(alert) is True
    assert workstation.state is WorkstationState.ALT
    workstation.acknowledge_alert(alert)
    assert workstation.state is WorkstationState.STD
    assert workstation.delivered_alerts == [1]


def test_unacknowledged_alert_is_discarded_after_wsalttime(workstation, clock):
    """Section 26.11.44: an alert is discarded after WsAltTime."""
    alert = Alert(text="stand change", entered_by="admin", alert_id=7)
    workstation.deliver_alert(alert)
    clock.advance(params.WS_ALT_TIME * BEFORE)
    assert workstation.state is WorkstationState.ALT
    clock.advance(params.WS_ALT_TIME * (AFTER - BEFORE) + 0.1)
    assert workstation.state is WorkstationState.STD
    assert workstation.alerts == []


def test_a_stopped_workstation_refuses_an_alert(bus, clock):
    """Section 6.2.1 sends alerts only to wStd, wBsy or wAlt."""
    ws = ManagedWorkstation("WS1", bus=bus, clock=clock)
    assert ws.deliver_alert(Alert("x", "admin", alert_id=1)) is False


def test_alert_returns_a_busy_workstation_to_busy(workstation, bus, clock):
    make_user(workstation, bus, clock)
    alert = Alert(text="x", entered_by="admin", alert_id=2)
    workstation.deliver_alert(alert)
    assert workstation.state is WorkstationState.ALT
    workstation.acknowledge_alert(alert)
    assert workstation.state is WorkstationState.BSY


# -- Chapter 8, user ------------------------------------------------------


def test_idle_user_times_out_to_the_screen_saver(workstation, bus, clock):
    """Section 26.11.43 UsrToTime, then section 8.1.6 into uSs."""
    user = make_user(workstation, bus, clock)
    clock.advance(params.USR_TO_TIME * BEFORE)
    assert user.state is UserState.STD
    clock.advance(params.USR_TO_TIME * (AFTER - BEFORE) + 0.1)
    assert user.state is UserState.SS


def test_screen_saver_logs_the_user_out_after_usrsstime(
    workstation, bus, clock
):
    """Section 26.11.42: uSs -> uSpg after UsrSsTime."""
    user = make_user(workstation, bus, clock)
    clock.advance(params.USR_TO_TIME * AFTER)
    assert user.state is UserState.SS
    clock.advance(params.USR_SS_TIME * BEFORE)
    assert user.state is UserState.SS
    clock.advance(params.USR_SS_TIME * (AFTER - BEFORE) + 0.1)
    assert user.state is UserState.SPG


def test_logout_that_overruns_usrspgtime_becomes_a_zombie(
    workstation, bus, clock
):
    """Section 26.11.41: uSpg -> uZom after UsrSpgTime."""
    user = make_user(workstation, bus, clock)
    user.log_out()
    clock.advance(params.USR_SPG_TIME * BEFORE)
    assert user.state is UserState.SPG
    clock.advance(params.USR_SPG_TIME * (AFTER - BEFORE) + 0.1)
    assert user.state is UserState.ZOM


def test_timeout_may_log_out_instead_of_showing_a_screen_saver(
    workstation, bus, clock
):
    """Section 8.1.6: the action is a matter of system configuration."""
    user = make_user(
        workstation, bus, clock, timeout_action=TimeoutAction.LOG_OUT
    )
    clock.advance(params.USR_TO_TIME * AFTER)
    assert user.state is UserState.SPG


def test_activity_resets_the_idle_timer(workstation, bus, clock):
    user = make_user(workstation, bus, clock)
    for _ in range(4):
        clock.advance(params.USR_TO_TIME * 0.6)
        user.activity()
    assert user.state is UserState.STD
    clock.advance(params.USR_TO_TIME * AFTER)
    assert user.state is UserState.SS


def test_activity_wakes_a_screen_saver(workstation, bus, clock):
    user = make_user(workstation, bus, clock)
    clock.advance(params.USR_TO_TIME * AFTER)
    assert user.state is UserState.SS
    user.activity()
    assert user.state is UserState.STD


def test_failed_logins_are_capped_at_usrathtries(workstation, bus, clock):
    """Section 26.11.40: UsrAthTries is 3 successive failures."""
    user = ManagedUser("BA-USER", workstation=workstation, bus=bus, clock=clock)
    outcomes = [user.record_failed_login() for _ in range(params.USR_ATH_TRIES)]
    assert outcomes[:-1] == [False] * (params.USR_ATH_TRIES - 1)
    assert outcomes[-1] is True


def test_successful_login_clears_the_failure_count(workstation, bus, clock):
    user = ManagedUser("BA-USER", workstation=workstation, bus=bus, clock=clock)
    user.record_failed_login()
    user.log_in()
    user.started()
    assert user.failed_logins == 0


def test_logging_out_a_user_stops_their_applications(workstation, bus, clock):
    user = make_user(workstation, bus, clock)
    application = ManagedApplication(
        1, "BAGATE", bus=bus, clock=clock, user=user
    )
    application.start()
    application.started()
    application.authenticated()
    assert application.state is ApplicationState.STD

    user.log_out()
    assert application.state is ApplicationState.SPG


def test_stopping_a_workstation_logs_its_users_out(workstation, bus, clock):
    user = make_user(workstation, bus, clock)
    workstation.stop()
    assert user.state is UserState.SPG


# -- Chapter 9, application -----------------------------------------------


def test_cupps_application_runs_through_aath_to_astd(bus, clock):
    application = ManagedApplication(1, "BAGATE", bus=bus, clock=clock)
    application.start()
    assert application.state is ApplicationState.STG
    application.started()
    assert application.state is ApplicationState.ATH
    application.authenticated(event_token="TOKEN")
    assert application.state is ApplicationState.STD
    assert application.event_token == "TOKEN"


def test_cots_application_goes_to_acts_not_aath(bus, clock):
    """Table 9.2: aStg reaches either aCts or aAth."""
    application = ManagedApplication(
        1, "NOTEPAD", bus=bus, clock=clock, is_cots=True
    )
    application.start()
    application.started()
    assert application.state is ApplicationState.CTS


def test_startup_that_overruns_appstgtime_becomes_a_zombie(bus, clock):
    """Section 26.11.3: AppStgTime bounds application startup."""
    application = ManagedApplication(1, "SLOWAPP", bus=bus, clock=clock)
    application.start()
    clock.advance(params.APP_STG_TIME * BEFORE)
    assert application.state is ApplicationState.STG
    clock.advance(params.APP_STG_TIME * (AFTER - BEFORE) + 0.1)
    assert application.state is ApplicationState.ZOM


def test_an_application_that_never_authenticates_is_directed_to_stop(bus, clock):
    """Section 9.1.1: aAth exists to catch exactly this."""
    application = ManagedApplication(
        1, "SILENTAPP", bus=bus, clock=clock, authenticate_within=20.0
    )
    application.start()
    application.started()
    assert application.state is ApplicationState.ATH
    clock.advance(20.0 * AFTER)
    assert application.state is ApplicationState.SPG


def test_stop_may_be_deferred_up_to_maxspgdefertimes(bus, clock):
    """Section 29.2 and section 26.11.20."""
    application = ManagedApplication(1, "BUSYAPP", bus=bus, clock=clock)
    application.start()
    application.started()
    application.authenticated()
    application.request_stop()

    granted = [application.defer_stop() for _ in range(params.MAX_SPG_DEFER_TIMES)]
    assert granted == [True] * params.MAX_SPG_DEFER_TIMES
    assert application.defer_stop() is False
    assert application.defer_count == params.MAX_SPG_DEFER_TIMES


def test_each_deferral_buys_another_appspgtime(bus, clock):
    application = ManagedApplication(1, "BUSYAPP", bus=bus, clock=clock)
    application.start()
    application.started()
    application.authenticated()
    application.request_stop()

    for _ in range(params.MAX_SPG_DEFER_TIMES):
        clock.advance(params.APP_SPG_TIME * 0.9)
        assert application.defer_stop() is True
        assert application.state is ApplicationState.SPG

    clock.advance(params.APP_SPG_TIME * AFTER)
    assert application.state is ApplicationState.ZOM


def test_an_application_that_will_not_stop_becomes_a_zombie(bus, clock):
    """Section 26.11.2: AppSpgTime bounds a directed shutdown."""
    application = ManagedApplication(1, "STUCKAPP", bus=bus, clock=clock)
    application.start()
    application.started()
    application.authenticated()
    application.request_stop()
    clock.advance(params.APP_SPG_TIME * BEFORE)
    assert application.state is ApplicationState.SPG
    clock.advance(params.APP_SPG_TIME * (AFTER - BEFORE) + 0.1)
    assert application.state is ApplicationState.ZOM


def test_clean_stop_reaches_astp(bus, clock):
    application = ManagedApplication(1, "GOODAPP", bus=bus, clock=clock)
    application.start()
    application.started()
    application.authenticated()
    application.request_stop()
    application.stopped()
    assert application.state is ApplicationState.STP
    clock.advance(params.APP_SPG_TIME * 3)
    assert application.state is ApplicationState.STP


def test_a_zombie_application_can_still_be_reaped(bus, clock):
    application = ManagedApplication(1, "STUCKAPP", bus=bus, clock=clock)
    application.start()
    application.started()
    application.authenticated()
    application.request_stop()
    clock.advance(params.APP_SPG_TIME * AFTER)
    assert application.state is ApplicationState.ZOM
    application.stopped()
    assert application.state is ApplicationState.STP


def test_application_events_carry_the_event_token(bus, clock):
    """Chapter 31 routes events by the application's eventToken."""
    received: list[str] = []
    bus.subscribe("TOKEN-A", lambda event: received.append(event.name))
    application = ManagedApplication(
        1, "BAGATE", bus=bus, clock=clock, event_token="TOKEN-A"
    )
    application.start()
    application.started()
    application.authenticated()
    assert "aStdEnteredEvent" in received


def test_no_timers_are_left_armed_after_a_clean_stop(bus, clock):
    application = ManagedApplication(1, "GOODAPP", bus=bus, clock=clock)
    application.start()
    application.started()
    application.authenticated()
    application.request_stop()
    application.stopped()
    assert application.pending_timers == []
