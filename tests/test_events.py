"""Platform events and subscriptions (TS 01.04.0004 chapter 31)."""

from __future__ import annotations

import pytest

from cuppsplatform.events import (
    NON_STATE_EVENTS,
    Event,
    EventBus,
    all_event_names,
    attach_state_machine,
    state_event_names,
)
from cuppsplatform.states import (
    ApplicationState,
    DeviceState,
    PlatformState,
    WorkstationState,
    new_machine,
)


# -- the event vocabulary -------------------------------------------------


def test_every_state_of_every_machine_has_entered_and_exited_events():
    names = state_event_names()
    for state in ("wStd", "uSs", "aAth", "dLkd", "pAlt"):
        assert f"{state}EnteredEvent" in names
        assert f"{state}ExitedEvent" in names


def test_platform_stopped_state_raises_no_events():
    """A stopped platform has nothing running to report it.

    Every other machine does have its stopped-state events, because a
    *running* platform reports them about its objects.
    """
    names = all_event_names()
    assert "pStpEnteredEvent" not in names
    assert "pStpExitedEvent" not in names
    for other in ("wStp", "uStp", "aStp", "dStp"):
        assert f"{other}EnteredEvent" in names, other
        assert f"{other}ExitedEvent" in names, other


def test_non_state_events_are_included():
    names = all_event_names()
    assert NON_STATE_EVENTS <= names
    for expected in (
        "deviceLockExpiredEvent",
        "devicePollingTooFastEvent",
        "illogicalMessageErrorEvent",
        "sessionErrorEvent",
        "exceptionEvent",
    ):
        assert expected in names


def test_state_events_are_distinguishable_from_the_rest():
    assert Event(name="dLkdEnteredEvent").is_state_event
    assert not Event(name="deviceLockExpiredEvent").is_state_event


# -- raising --------------------------------------------------------------


def test_state_changes_become_events_in_order():
    bus = EventBus()
    seen: list[str] = []
    bus.add_listener(lambda event: seen.append(event.name))

    machine = new_machine("device", subject="LHRT4PR1")
    attach_state_machine(bus, machine)
    machine.enter(DeviceState.STG)
    machine.enter(DeviceState.STD)

    assert seen == [
        "dStpExitedEvent", "dStgEnteredEvent",
        "dStgExitedEvent", "dStdEnteredEvent",
    ]


def test_platform_start_suppresses_only_the_stopped_events():
    bus = EventBus()
    seen: list[str] = []
    bus.add_listener(lambda event: seen.append(event.name))

    machine = new_machine("platform")
    attach_state_machine(bus, machine)
    machine.enter(PlatformState.STG)

    # pStpExitedEvent is suppressed; pStgEnteredEvent is not.
    assert seen == ["pStgEnteredEvent"]


def test_event_carries_its_subject_and_reason():
    bus = EventBus()
    captured: list[Event] = []
    bus.add_listener(captured.append)

    machine = new_machine("workstation", subject="LHRT4LB00302")
    attach_state_machine(bus, machine)
    machine.enter(WorkstationState.STG, reason="platform start")

    assert captured[-1].subject == "LHRT4LB00302"
    assert captured[-1].attributes["reason"] == "platform start"
    assert captured[-1].attributes["machine"] == "workstation"


# -- subscriptions (section 31.1) -----------------------------------------


def test_subscriber_receives_only_its_own_token():
    bus = EventBus()
    mine: list[str] = []
    theirs: list[str] = []
    bus.subscribe("TOKEN-A", lambda event: mine.append(event.name))
    bus.subscribe("TOKEN-B", lambda event: theirs.append(event.name))

    bus.raise_event(Event(name="exceptionEvent", event_token="TOKEN-A"))
    assert mine == ["exceptionEvent"]
    assert theirs == []


def test_a_management_application_may_subscribe_to_an_unseen_token():
    """Section 31.1 requires this, or a monitor could only start second."""
    bus = EventBus()
    received: list[str] = []
    subscriber = bus.subscribe("NEVER-PRESENTED", lambda e: received.append(e.name))
    assert subscriber > 0
    assert received == []
    # ...and it starts receiving once the token is actually used.
    bus.raise_event(Event(name="aStdEnteredEvent", event_token="NEVER-PRESENTED"))
    assert received == ["aStdEnteredEvent"]


def test_two_applications_sharing_a_token_both_receive():
    bus = EventBus()
    first: list[str] = []
    second: list[str] = []
    bus.subscribe("SHARED", lambda e: first.append(e.name))
    bus.subscribe("SHARED", lambda e: second.append(e.name))
    bus.raise_event(Event(name="aSpgEnteredEvent", event_token="SHARED"))
    assert first == second == ["aSpgEnteredEvent"]


def test_empty_subscription_token_is_refused():
    with pytest.raises(ValueError, match="must not be empty"):
        EventBus().subscribe("", lambda event: None)


def test_unsubscribe_and_list():
    bus = EventBus()
    bus.subscribe("TOKEN-A", lambda e: None)
    bus.subscribe("TOKEN-B", lambda e: None)
    assert bus.subscriptions() == ["TOKEN-A", "TOKEN-B"]
    assert bus.unsubscribe("TOKEN-A") == 1
    assert bus.subscriptions() == ["TOKEN-B"]
    assert bus.unsubscribe("TOKEN-A") == 0


def test_dropping_a_session_removes_all_its_subscriptions():
    bus = EventBus()
    subscriber = bus.subscribe("TOKEN-A", lambda e: None)
    bus.subscribe("TOKEN-B", lambda e: None)
    other = bus.subscribe("TOKEN-A", lambda e: None)
    assert bus.drop_subscriber(subscriber) == 1
    assert bus.subscription_count == 2
    assert bus.drop_subscriber(other) == 1


def test_listeners_see_every_event_regardless_of_token():
    """Section 10.3 requires a log entry for every device state change."""
    bus = EventBus()
    logged: list[str] = []
    bus.add_listener(lambda event: logged.append(event.name))
    bus.raise_event(Event(name="dErrEnteredEvent", event_token="ANY"))
    bus.raise_event(Event(name="dStdEnteredEvent", event_token="OTHER"))
    assert logged == ["dErrEnteredEvent", "dStdEnteredEvent"]


def test_a_failing_subscriber_does_not_stop_the_others():
    """Delivery must not unwind the transition that raised the event."""
    bus = EventBus()
    delivered: list[str] = []

    def explode(event: Event) -> None:
        raise RuntimeError("subscriber is broken")

    bus.subscribe("TOKEN", explode)
    bus.subscribe("TOKEN", lambda e: delivered.append(e.name))
    bus.add_listener(explode)

    event = bus.raise_event(Event(name="exceptionEvent", event_token="TOKEN"))
    assert event is not None
    assert delivered == ["exceptionEvent"]


def test_a_failing_listener_does_not_break_a_state_transition():
    bus = EventBus()
    bus.add_listener(lambda event: (_ for _ in ()).throw(RuntimeError("boom")))
    machine = new_machine("application", subject="APP1")
    attach_state_machine(bus, machine)
    machine.enter(ApplicationState.STG)
    assert machine.state is ApplicationState.STG


def test_raise_event_tolerates_none():
    """So a caller can pass Event.from_state_change straight through."""
    assert EventBus().raise_event(None) is None


def test_recent_events_are_bounded():
    bus = EventBus()
    for index in range(1500):
        bus.raise_event(Event(name="exceptionEvent", attributes={"n": index}))
    assert len(bus.recent(limit=10_000)) <= 1000
