"""Tests for the self-service conformance harness.

A harness that cannot fail an application is a rubber stamp, so the core of
this file drives a deliberately non-conforming application and asserts that
each planted violation is caught by the right check.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conformance import ConformanceHarness, catalogue, to_dict, to_html, to_json, to_text
from conformance.checks import FAIL, NOT_EXERCISED, PASS, REQUIRED
from conformance.harness import ScenarioStep

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "nonconforming_app.py"


def _quick_scenario(harness: ConformanceHarness) -> list[ScenarioStep]:
    """The default steps with the waiting trimmed, to keep the suite fast."""
    steps = harness.default_scenario()
    for step in steps:
        step.settle = 1.0 if step.name != "platform-loss" else 12.0
    return steps


def _outcome(findings, check_id: str) -> str:
    for finding in findings:
        if finding.check_id == check_id:
            return finding.outcome
    raise AssertionError(f"no finding for {check_id}")


@pytest.fixture(scope="module")
def nonconforming_result():
    """Run the harness against the deliberately broken application."""
    harness = ConformanceHarness(
        application_name="NONCONFORMING", application_version="00.01"
    ).start()
    environment = dict(os.environ)
    environment.update(harness.environment)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["BADAPP_LINGER"] = "20"

    child = subprocess.Popen(
        [sys.executable, str(FIXTURE)],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert harness.wait_for_application(timeout=45), "fixture never authenticated"
        harness.wait_for_devices(count=2, timeout=20)
        time.sleep(3)
        yield harness.evaluate(["observed"])
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        harness.stop()


@pytest.mark.parametrize(
    "check_id,why",
    [
        ("APP-0020", "authenticates twice on one connection"),
        ("APP-0060", "acquires a device without setting the interface mode"),
        ("APP-0064", "locks a Special Mode device"),
        ("APP-0065", "polls status far faster than DevPollMaxFreq"),
    ],
)
def test_planted_violations_are_caught(nonconforming_result, check_id, why):
    assert _outcome(nonconforming_result.findings, check_id) == FAIL, why


def test_nonconforming_application_does_not_pass(nonconforming_result):
    assert not nonconforming_result.passed
    assert nonconforming_result.blocking_failures


def test_conforming_application_passes(simulator):
    """The reference application must pass its own harness."""
    from cupps import CuppsEnvironment
    from cuppsd.service import AppState, CuppsService, ServiceConfig

    harness = ConformanceHarness(
        application_name="CUPPSAGENT", application_version="01.00"
    ).start()
    try:
        environment = CuppsEnvironment.from_os(harness.environment)
        config = ServiceConfig.from_environment(
            environment, application_name="CUPPSAGENT"
        )
        service = CuppsService(config)
        service.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and service.state is not AppState.STD:
            time.sleep(0.1)
        assert service.state is AppState.STD

        steps_run = harness.run_scenario(_quick_scenario(harness))
        service.stop(timeout=10)
        time.sleep(1.5)

        result = harness.evaluate(steps_run)
        failures = [
            f"{f.check_id} {f.title}: {f.detail}"
            for f in result.findings
            if f.outcome == FAIL and f.severity == REQUIRED
        ]
        assert not failures, "the reference application failed its own harness: " + \
            "; ".join(failures)
        # Recovery is the check section 17.1.2 singles out; it must be covered.
        assert _outcome(result.findings, "APP-0070") == PASS
    finally:
        harness.stop()


def test_catalogue_is_well_formed():
    ids = [check.check_id for check in catalogue()]
    assert len(ids) == len(set(ids)), "duplicate check ids"
    for check in catalogue():
        assert check.check_id.startswith("APP-")
        assert check.section, f"{check.check_id} cites no specification section"
        assert check.severity in ("required", "recommended", "informational")


def test_reports_render_in_every_format(nonconforming_result):
    text = to_text(nonconforming_result)
    assert "SELF-ASSESSMENT RECORD" in text
    assert "not a certificate" in text

    payload = json.loads(to_json(nonconforming_result))
    assert payload["verdict"]["passed"] is False
    assert payload["findings"]
    assert payload["conversation"], "the record must carry the logged conversation"
    assert "disclaimer" in payload

    html = to_html(nonconforming_result)
    assert html.startswith("<!DOCTYPE html>")
    assert "not a certificate" in html
    # Self-contained: no external assets to lose when it is emailed on.
    assert "src=http" not in html and "href=\"http" not in html


def test_fingerprint_is_stable(nonconforming_result):
    from conformance.report import fingerprint

    assert fingerprint(nonconforming_result) == fingerprint(nonconforming_result)
    assert len(fingerprint(nonconforming_result)) == 16


def test_record_states_what_was_not_exercised(simulator):
    """A check that never ran must be reported, not silently counted as a pass."""
    harness = ConformanceHarness().start()
    try:
        result = harness.evaluate([])
        not_exercised = {f.check_id for f in result.findings if f.outcome == NOT_EXERCISED}
        assert "APP-0070" in not_exercised
        assert "NOT EXERCISED" in to_text(result)
    finally:
        harness.stop()
