"""Device management and locking (TS 01.04.0004 chapter 10, section 26.13).

The centrepiece is the worked example the specification prints as Figure 26.7
and describes step by step in section 26.13.1. Encoding a published example
directly is the strongest check available: if the implementation disagrees
with it, one of the two is wrong and it is worth knowing which.
"""

from __future__ import annotations

import pytest

from cupps import params
from cupps.results import InterfaceMode, LockMethod

from cuppsplatform.clock import ManualClock
from cuppsplatform.device import (
    AcquiredSession,
    LockingNotSupported,
    LockRefused,
    ManagedDevice,
)
from cuppsplatform.events import Event, EventBus
from cuppsplatform.states import DeviceState


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def clock():
    return ManualClock()


def make_device(bus, clock, name="LHRT4LB00302BC1", device_type="BC"):
    device = ManagedDevice(name, device_type, bus=bus, clock=clock)
    device.start()
    device.started()
    return device


def make_session(session_id, token, notes=None, mode=InterfaceMode.STANDARD):
    sink = notes if notes is not None else []
    return AcquiredSession(
        session_id=session_id,
        device_token=token,
        airline_id="BA",
        interface_mode=mode,
        notify=lambda event, s=sink, i=session_id: s.append((i, event.name)),
        application_name=f"APP{session_id}",
    )


# -- lifecycle (chapter 10) -----------------------------------------------


def test_device_starts_through_dstg_to_dstd(bus, clock):
    device = ManagedDevice("PR1", "PR", bus=bus, clock=clock)
    assert device.state is DeviceState.STP
    device.start()
    assert device.state is DeviceState.STG
    device.started()
    assert device.state is DeviceState.STD


def test_startup_that_overruns_devstgtime_becomes_a_zombie(bus, clock):
    """Section 26.11.14: dStg -> dZom when startup does not complete."""
    device = ManagedDevice("PR1", "PR", bus=bus, clock=clock)
    device.start()
    clock.advance(params.DEV_STG_TIME * 1.02)
    assert device.state is DeviceState.ZOM


def test_shutdown_that_overruns_devspgtime_becomes_a_zombie(bus, clock):
    """Section 26.11.13: dSpg -> dZom."""
    device = make_device(bus, clock)
    device.stop()
    assert device.state is DeviceState.SPG
    clock.advance(params.DEV_SPG_TIME * 1.02)
    assert device.state is DeviceState.ZOM


def test_clean_shutdown_reaches_dstp(bus, clock):
    device = make_device(bus, clock)
    device.stop()
    device.stopped()
    assert device.state is DeviceState.STP
    clock.advance(params.DEV_SPG_TIME * 2)
    assert device.state is DeviceState.STP, "a stopped device must not zombie"


def test_fault_and_recovery(bus, clock):
    device = make_device(bus, clock)
    device.fault("paper out")
    assert device.state is DeviceState.ERR
    device.cleared()
    assert device.state is DeviceState.STD


# -- the Figure 26.7 worked example ---------------------------------------


def test_figure_26_7_lock_sequence(bus, clock):
    """Section 26.13.1, steps 1 to 14, byDeviceToken versus byConnection."""
    device = make_device(bus, clock)
    notes: list[tuple[int, str]] = []
    app_a = make_session(1, "token-a", notes)
    app_b = make_session(2, "token-b", notes)
    device.acquire(app_a)
    device.acquire(app_b)

    # (1) Application A locks BC1 using device token A.
    assert device.lock(app_a, LockMethod.BY_DEVICE_TOKEN) == "OK"

    # (2) A attempts to lock using token B -- refused, already locked token A.
    app_a_as_b = make_session(1, "token-b", notes)
    with pytest.raises(LockRefused) as refused:
        device.lock(app_a_as_b, LockMethod.BY_DEVICE_TOKEN)
    assert refused.value.result == "deviceLocked"

    # (3) B attempts byConnection -- refused, a byDeviceToken lock is held.
    with pytest.raises(LockRefused) as refused:
        device.lock(app_b, LockMethod.BY_CONNECTION)
    assert refused.value.result == "switchingLockMethodsNotAllowed"

    # (4) B attempts using token B -- refused.
    with pytest.raises(LockRefused) as refused:
        device.lock(app_b, LockMethod.BY_DEVICE_TOKEN)
    assert refused.value.result == "deviceLocked"

    # (5) B attempts using token A -- granted; A and B now share the lock.
    app_b_on_token_a = make_session(2, "token-a", notes)
    assert device.lock(app_b_on_token_a, LockMethod.BY_DEVICE_TOKEN) == "OK"

    # (9) A releases its lock; B still holds it through token A.
    device.unlock(app_a)
    assert device.locked, "the token lock survives while another session holds it"

    # (12) B unlocks; the device is now free.
    device.unlock(app_b_on_token_a)
    assert not device.locked
    assert device.state is DeviceState.STD

    # (13) A locks byConnection.
    assert device.lock(app_a, LockMethod.BY_CONNECTION) == "OK"

    # (14) B attempts byConnection -- refused, A holds it.
    with pytest.raises(LockRefused) as refused:
        device.lock(app_b, LockMethod.BY_CONNECTION)
    assert refused.value.result == "deviceLocked"


def test_renewing_a_held_lock_is_granted(bus, clock):
    """Section 30.7 REQUIREMENT, and the OK-deviceAlreadyLocked result code.

    Step 11 of the Figure 26.7 narrative says a second lock request from a
    session that already holds the lock is rejected. That contradicts the
    REQUIREMENT box in section 30.7 -- "the platform must grant the
    application's request to renew the lock and reset its timer" -- and the
    existence of an ``OK-deviceAlreadyLocked`` *success* result for exactly
    this case. The normative requirement is followed here.
    """
    device = make_device(bus, clock)
    session = make_session(1, "token-a")
    device.acquire(session)
    assert device.lock(session, LockMethod.BY_CONNECTION) == "OK"
    assert device.lock(session, LockMethod.BY_CONNECTION) == "OK-deviceAlreadyLocked"


def test_lock_refusal_reports_the_holder(bus, clock):
    """Section 30.7 requires <deviceLockerInfo> on a deviceLocked refusal."""
    device = make_device(bus, clock)
    first = make_session(1, "token-a")
    second = make_session(2, "token-b")
    device.acquire(first)
    device.acquire(second)
    device.lock(first, LockMethod.BY_CONNECTION)
    with pytest.raises(LockRefused) as refused:
        device.lock(second, LockMethod.BY_CONNECTION)
    assert refused.value.locker["airline"] == "BA"
    assert refused.value.locker["applicationName"] == "APP1"


# -- expiry versus release (section 26.13.2) ------------------------------


def test_lock_expiry_notifies_every_acquiring_application(bus, clock):
    """Section 26.13.2: all applications holding the device are told."""
    device = make_device(bus, clock)
    notes: list[tuple[int, str]] = []
    holder = make_session(1, "token-a", notes)
    watcher = make_session(2, "token-b", notes)
    device.acquire(holder)
    device.acquire(watcher)
    device.lock(holder, LockMethod.BY_CONNECTION)

    clock.advance(params.DEV_LKD_TIME * 1.02)

    assert device.state is DeviceState.STD
    assert not device.locked
    assert sorted(notes) == [
        (1, "deviceLockExpiredEvent"),
        (2, "deviceLockExpiredEvent"),
    ]


def test_explicit_unlock_notifies_nobody(bus, clock):
    """Section 26.13.2: sessions on the same token are assumed to know."""
    device = make_device(bus, clock)
    notes: list[tuple[int, str]] = []
    holder = make_session(1, "token-a", notes)
    other = make_session(2, "token-b", notes)
    device.acquire(holder)
    device.acquire(other)
    device.lock(holder, LockMethod.BY_CONNECTION)
    device.unlock(holder)
    assert notes == [], "an explicit unlock must not raise a lock expired event"


def test_device_io_resets_the_expiry_timer(bus, clock):
    device = make_device(bus, clock)
    notes: list[tuple[int, str]] = []
    session = make_session(1, "token-a", notes)
    device.acquire(session)
    device.lock(session, LockMethod.BY_CONNECTION)

    for _ in range(3):
        clock.advance(params.DEV_LKD_TIME * 0.6)
        device.touch()
    assert device.locked, "I/O should have kept the lock alive"

    clock.advance(params.DEV_LKD_TIME * 1.02)
    assert not device.locked


def test_release_drops_the_lock_without_an_event(bus, clock):
    device = make_device(bus, clock)
    notes: list[tuple[int, str]] = []
    session = make_session(1, "token-a", notes)
    device.acquire(session)
    device.lock(session, LockMethod.BY_CONNECTION)
    device.release(session.session_id)
    assert not device.locked
    assert notes == []


# -- dBsy and the AEA asymmetry -------------------------------------------


def test_standard_mode_device_moves_through_dbsy(bus, clock):
    device = make_device(bus, clock)
    session = make_session(1, "token-a", mode=InterfaceMode.STANDARD)
    device.acquire(session)
    device.lock(session)
    device.begin_operation()
    assert device.state is DeviceState.BSY
    device.end_operation()
    assert device.state is DeviceState.LKD


def test_aea_mode_device_stays_in_dlkd(bus, clock):
    """Table 10.1 footnote: an AEA device does not enter dBsy."""
    device = make_device(bus, clock, name="LHRBP1", device_type="BP")
    session = make_session(1, "token-a", mode=InterfaceMode.AEA)
    device.acquire(session)
    device.lock(session)
    assert device.state is DeviceState.LKD
    device.begin_operation()
    assert device.state is DeviceState.LKD, "an AEA device must remain in dLkd"


# -- Special Mode devices (section 30.20) ---------------------------------


@pytest.mark.parametrize("device_type", ["ZL", "ZI"])
def test_special_mode_devices_cannot_be_locked(bus, clock, device_type):
    device = make_device(bus, clock, name=f"LHR{device_type}1",
                         device_type=device_type)
    session = make_session(1, "token-a")
    device.acquire(session)
    with pytest.raises(LockingNotSupported):
        device.lock(session)
    with pytest.raises(LockingNotSupported):
        device.unlock(session)
    assert device.state is DeviceState.STD, "the device must remain usable"


# -- macro devices (section 26.13.3) --------------------------------------


def test_locking_a_macro_device_locks_its_sub_devices(bus, clock):
    gate = ManagedDevice("LHRBG1", "BG", bus=bus, clock=clock)
    barcode = gate.add_sub_device(
        ManagedDevice("LHRBC2", "BC", bus=bus, clock=clock)
    )
    stripe = gate.add_sub_device(
        ManagedDevice("LHRMS2", "MS", bus=bus, clock=clock)
    )
    gate.start()
    for device in gate.walk():
        device.started()

    owner = make_session(1, "token-a")
    gate.acquire(owner)
    gate.lock(owner)

    assert barcode.state is DeviceState.LKD
    assert stripe.state is DeviceState.LKD

    # Another application cannot take a sub-device out from under it.
    intruder = make_session(2, "token-b")
    barcode.acquire(intruder)
    with pytest.raises(LockRefused):
        barcode.lock(intruder)

    gate.unlock(owner)
    assert barcode.state is DeviceState.STD
    assert stripe.state is DeviceState.STD


def test_sub_device_lock_survives_the_macro_unlock_when_held_explicitly(bus, clock):
    """Section 26.13.3 steps 8 to 15: an explicit sub-device lock is its own."""
    gate = ManagedDevice("LHRBG1", "BG", bus=bus, clock=clock)
    barcode = gate.add_sub_device(
        ManagedDevice("LHRBC2", "BC", bus=bus, clock=clock)
    )
    gate.start()
    for device in gate.walk():
        device.started()

    owner = make_session(1, "token-a")
    gate.acquire(owner)
    barcode.acquire(owner)
    gate.lock(owner)
    barcode.lock(owner)          # explicit, by the same owner

    gate.unlock(owner)
    assert barcode.locked, "an explicitly held sub-device lock must survive"


# -- events ---------------------------------------------------------------


def test_state_changes_reach_the_bus(bus, clock):
    """Section 10.3 requires a log entry for every device state entry/exit."""
    seen: list[str] = []
    bus.add_listener(lambda event: seen.append(event.name))
    device = make_device(bus, clock)
    session = make_session(1, "token-a")
    device.acquire(session)
    device.lock(session)
    assert "dStgEnteredEvent" in seen
    assert "dStdEnteredEvent" in seen
    assert "dLkdEnteredEvent" in seen


def test_a_stalled_application_does_not_freeze_the_device(bus, clock):
    """Notifications must not be delivered while the device lock is held.

    Lock expiry notifies every acquiring application. If that delivery runs
    inside the device's own mutex, one application whose socket has stopped
    draining blocks every other application from touching the device -- a
    shared boarding pass printer frozen by one wedged gate client. Delivery
    therefore happens after the device's state is already consistent and its
    mutex released.
    """
    import threading

    device = make_device(bus, clock)
    stalled = threading.Event()
    release = threading.Event()

    def slow_notify(event):
        stalled.set()
        release.wait(timeout=5)

    slow = AcquiredSession(
        session_id=1, device_token="token-a", airline_id="BA",
        interface_mode=InterfaceMode.STANDARD, notify=slow_notify,
        application_name="SLOW",
    )
    device.acquire(slow)
    device.lock(slow, LockMethod.BY_CONNECTION)

    expiry = threading.Thread(
        target=lambda: clock.advance(params.DEV_LKD_TIME * 1.02), daemon=True
    )
    expiry.start()
    assert stalled.wait(timeout=5), "the notification never ran"

    # While that application is wedged mid-delivery, the device must still
    # answer other callers.
    answered = threading.Event()

    def other_caller():
        _ = device.state
        _ = device.locked
        _ = device.acquired_by
        answered.set()

    threading.Thread(target=other_caller, daemon=True).start()
    assert answered.wait(timeout=3), (
        "the device was frozen by a stalled application's notification"
    )

    release.set()
    expiry.join(timeout=5)
