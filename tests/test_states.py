"""The five CUPPS state machines (TS 01.04.0004, Part II).

The transition tables are the specification's own, so a transcription error
would silently invalidate everything built on top. To catch that, each table
is written out here a second time in the grid form the specification prints
it -- rows are "from", columns are "to", ``X`` allowed, ``-`` not possible,
``—`` not applicable -- and cross-checked against the dictionaries the engine
actually uses. Two independent transcriptions disagreeing is the signal.
"""

from __future__ import annotations

import pytest

from cuppsplatform.states import (
    APPLICATION_TRANSITIONS,
    DEVICE_TRANSITIONS,
    MACHINES,
    PLATFORM_TRANSITIONS,
    USER_TRANSITIONS,
    WORKSTATION_TRANSITIONS,
    ApplicationState,
    DeviceState,
    IllegalTransition,
    PlatformState,
    StateMachine,
    UserState,
    WorkstationState,
    new_machine,
)


def parse_grid(columns: list[str], rows: dict[str, str]) -> dict[str, set[str]]:
    """Turn the specification's printed grid into {from: {to, ...}}."""
    table: dict[str, set[str]] = {}
    for source, cells in rows.items():
        marks = cells.split()
        assert len(marks) == len(columns), (
            f"row {source} has {len(marks)} cells, expected {len(columns)}"
        )
        allowed = set()
        for target, mark in zip(columns, marks):
            if mark == "X":
                allowed.add(target)
            elif mark == "=":            # the specification's em dash: same state
                assert target == source, f"{source}: '=' must be the diagonal"
            else:
                assert mark == "-", f"unexpected mark {mark!r} in row {source}"
        table[source] = allowed
    return table


def as_plain(transitions: dict) -> dict[str, set[str]]:
    """Flatten an enum-keyed transition dict to plain strings."""
    return {
        source.value: {target.value for target in targets}
        for source, targets in transitions.items()
    }


# -- Table 6.3, Platform State Transitions --------------------------------

PLATFORM_GRID = parse_grid(
    ["pStp", "pStg", "pStd", "pAlt", "pSpg"],
    {
        "pStp": "=    X    -    -    -",
        "pStg": "-    =    X    -    X",
        "pStd": "-    -    =    X    X",
        "pAlt": "-    -    X    =    X",
        "pSpg": "X    -    -    -    =",
    },
)

# -- Table 7.2, Workstation State Transitions -----------------------------

WORKSTATION_GRID = parse_grid(
    ["wStp", "wStg", "wStd", "wBsy", "wAlt", "wSpg", "wZom"],
    {
        "wStp": "=    X    -    -    -    -    -",
        "wStg": "-    =    X    -    -    X    X",
        "wStd": "-    -    =    X    X    X    X",
        "wBsy": "-    -    X    =    X    X    X",
        "wAlt": "-    -    X    X    =    X    X",
        "wSpg": "X    -    -    -    -    =    X",
        "wZom": "X    -    -    -    -    -    =",
    },
)

# -- Table 8.2, User State Transitions ------------------------------------

USER_GRID = parse_grid(
    ["uStp", "uStg", "uStd", "uTo", "uSs", "uSpg", "uZom"],
    {
        "uStp": "=    X    -    -    -    -    -",
        "uStg": "-    =    X    -    -    X    X",
        "uStd": "-    -    =    X    X    X    X",
        "uTo":  "-    -    -    =    X    X    X",
        "uSs":  "-    -    X    -    =    X    X",
        "uSpg": "X    -    -    -    -    =    X",
        "uZom": "X    -    -    -    -    -    =",
    },
)

# -- Table 9.2, Application State Transitions -----------------------------

APPLICATION_GRID = parse_grid(
    ["aStp", "aStg", "aCts", "aAth", "aStd", "aSpg", "aZom"],
    {
        "aStp": "=    X    -    -    -    -    -",
        "aStg": "-    =    X    X    -    X    X",
        "aCts": "-    -    =    -    -    X    X",
        "aAth": "-    -    -    =    X    X    X",
        "aStd": "-    -    -    -    =    X    X",
        "aSpg": "X    -    -    -    -    =    X",
        "aZom": "X    -    -    -    -    -    =",
    },
)

# -- Table 10.2, Device State Transitions ---------------------------------

DEVICE_GRID = parse_grid(
    ["dStp", "dStg", "dStd", "dLkd", "dBsy", "dErr", "dSpg", "dZom"],
    {
        "dStp": "=    X    -    -    -    -    -    -",
        "dStg": "-    =    X    -    -    -    X    X",
        "dStd": "-    -    =    X    -    X    X    X",
        "dLkd": "-    -    X    =    X    X    X    X",
        "dBsy": "-    -    -    X    =    X    X    X",
        "dErr": "-    -    X    X    X    =    X    X",
        "dSpg": "X    -    -    -    -    -    =    X",
        "dZom": "X    -    -    -    -    -    -    =",
    },
)


@pytest.mark.parametrize(
    "name,engine,grid",
    [
        ("platform", PLATFORM_TRANSITIONS, PLATFORM_GRID),
        ("workstation", WORKSTATION_TRANSITIONS, WORKSTATION_GRID),
        ("user", USER_TRANSITIONS, USER_GRID),
        ("application", APPLICATION_TRANSITIONS, APPLICATION_GRID),
        ("device", DEVICE_TRANSITIONS, DEVICE_GRID),
    ],
)
def test_transition_table_matches_the_specification(name, engine, grid):
    assert as_plain(engine) == grid, (
        f"the {name} transition table does not match the specification grid"
    )


@pytest.mark.parametrize("name", sorted(MACHINES))
def test_every_state_appears_in_the_table(name):
    definition = MACHINES[name]
    covered = set(definition.transitions)
    assert covered == set(definition.states), (
        f"{name}: states {set(definition.states) - covered} have no row"
    )
    # Every target must itself be a state of this machine.
    for source, targets in definition.transitions.items():
        for target in targets:
            assert target in set(definition.states), f"{source} -> {target}"


@pytest.mark.parametrize("name", sorted(MACHINES))
def test_default_state_is_the_stopped_state(name):
    """Every chapter names its stopped state the default."""
    definition = MACHINES[name]
    assert definition.initial.value.endswith("Stp")


@pytest.mark.parametrize("name", sorted(MACHINES))
def test_every_state_is_reachable_from_the_default(name):
    definition = MACHINES[name]
    reached = {definition.initial}
    frontier = [definition.initial]
    while frontier:
        current = frontier.pop()
        for target in definition.transitions.get(current, frozenset()):
            if target not in reached:
                reached.add(target)
                frontier.append(target)
    unreachable = set(definition.states) - reached
    assert not unreachable, f"{name}: unreachable states {unreachable}"


@pytest.mark.parametrize("name", sorted(MACHINES))
def test_every_state_can_return_to_stopped(name):
    """A machine that cannot be shut down would strand the platform."""
    definition = MACHINES[name]
    reaches_stop = {definition.initial}
    changed = True
    while changed:
        changed = False
        for source, targets in definition.transitions.items():
            if source in reaches_stop:
                continue
            if targets & reaches_stop:
                reaches_stop.add(source)
                changed = True
    stranded = set(definition.states) - reaches_stop
    assert not stranded, f"{name}: states that cannot reach stopped: {stranded}"


# -- behaviour ------------------------------------------------------------


def test_exit_is_fired_before_the_next_state_is_entered():
    """Section 6.2.1: the exit event fires before the next state is entered."""
    machine = new_machine("platform")
    events: list[str] = []
    machine.add_listener(lambda change: events.append(change.event_name))
    machine.enter(PlatformState.STG)
    assert events == ["pStpExitedEvent", "pStgEnteredEvent"]


def test_event_names_match_the_specification():
    machine = new_machine("device", subject="LHRT4PR1")
    machine.enter(DeviceState.STG)
    machine.enter(DeviceState.STD)
    machine.enter(DeviceState.LKD)
    names = [change.event_name for change in machine.history()]
    assert names == [
        "dStpExitedEvent", "dStgEnteredEvent",
        "dStgExitedEvent", "dStdEnteredEvent",
        "dStdExitedEvent", "dLkdEnteredEvent",
    ]


def test_illegal_transition_is_refused():
    machine = new_machine("application")
    machine.enter(ApplicationState.STG)
    machine.enter(ApplicationState.ATH)
    # Table 9.2: aAth may reach aStd, aSpg or aZom -- never aCts.
    with pytest.raises(IllegalTransition, match="aAth -> aCts"):
        machine.enter(ApplicationState.CTS)
    assert machine.state is ApplicationState.ATH, "state must not move on refusal"


def test_force_is_reported_and_still_emits_both_events(caplog):
    machine = new_machine("workstation", subject="WS1")
    machine.enter(WorkstationState.STG)
    with caplog.at_level("WARNING"):
        machine.force(WorkstationState.BSY, reason="workstation vanished")
    assert machine.state is WorkstationState.BSY
    assert "forcing" in caplog.text
    kinds = [c.kind for c in machine.history()[-2:]]
    assert kinds == ["exited", "entered"]


def test_force_uses_the_normal_path_when_the_transition_is_legal(caplog):
    machine = new_machine("platform")
    with caplog.at_level("WARNING"):
        machine.force(PlatformState.STG, reason="startup")
    assert "forcing" not in caplog.text


def test_subject_is_carried_on_every_change():
    machine = new_machine("device", subject="LHRT4LB00302BC1")
    machine.enter(DeviceState.STG)
    assert all(c.subject == "LHRT4LB00302BC1" for c in machine.history())


def test_unknown_machine_name_lists_the_real_ones():
    with pytest.raises(KeyError, match="platform"):
        new_machine("nosuchmachine")


def test_history_is_bounded():
    machine = new_machine("device")
    for _ in range(200):
        machine.enter(DeviceState.STG)
        machine.enter(DeviceState.STD)
        machine.enter(DeviceState.SPG)
        machine.enter(DeviceState.STP)
    assert len(machine.history(limit=10_000)) <= 500
