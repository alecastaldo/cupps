"""Shared fixtures for the conformance tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simulator import PlatformSimulator  # noqa: E402


@pytest.fixture
def simulator():
    """A running platform simulator on an ephemeral port."""
    sim = PlatformSimulator(workstation="TSTCUPPSCKI001").start()
    try:
        yield sim
    finally:
        sim.stop()


@pytest.fixture
def platform(simulator):
    """An authenticated platform session against the simulator."""
    from cupps import PlatformSession

    session = PlatformSession(
        simulator.host,
        simulator.platform_port,
        airline="ZZ",
        event_token="TESTTOKEN0000001",
        applications=[("TESTAPP", "01.00")],
    )
    session.open()
    try:
        yield session
    finally:
        session.close()
