"""The self-service Application Compliance harness.

Stands up an instrumented CUPPS platform, lets the application under test
connect to it, drives the scenarios the specification calls out, records the
whole conversation and evaluates the check catalogue against it.

This produces a **Compliance Testing Record** in the sense of Figure 12.1 --
logged results plus a pass/fail comparison.  It is *evidence*, not a
certificate: section 17.1.2 reserves Application Compliance Testing to a
compliant platform supplier or the IATA approved CTE.  The point is to make
that formal test a review of evidence rather than a discovery exercise, and
to catch interface faults before they consume an integration attempt (two
failures across multiple suppliers force full re-compliance, §17.1.2).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from simulator import PlatformSimulator, SimulatedDevice, default_devices

from .checks import Finding, run_all
from .recorder import Recorder

log = logging.getLogger("conformance.harness")

#: A Resolution 792 record built to exact field widths, for scan injection.
SAMPLE_BOARDING_PASS = (
    "M1" + "CONFORMANCE/TEST".ljust(20) + "E" + "ZZ0001 ".ljust(7)
    + "LHRJFKZZ " + "00001" + "001" + "Y" + "001A" + "00001" + "1" + "00"
)


@dataclass
class ScenarioStep:
    """One driven step of a conformance run."""

    name: str
    description: str
    run: Callable[["ConformanceHarness"], None]
    #: Seconds to wait after the step so the application can react.
    settle: float = 2.0


@dataclass
class RunResult:
    """Everything a report needs from one harness run."""

    started: str
    finished: str
    duration: float
    findings: list[Finding]
    recorder: Recorder
    steps_run: list[str] = field(default_factory=list)
    application_name: str = ""
    application_version: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def blocking_failures(self) -> list[Finding]:
        return [f for f in self.findings if f.blocking]

    @property
    def advisory_failures(self) -> list[Finding]:
        return [
            f
            for f in self.findings
            if f.outcome == "fail" and f.severity == "recommended"
        ]

    @property
    def not_exercised(self) -> list[Finding]:
        return [f for f in self.findings if f.outcome == "not-exercised"]

    @property
    def passed(self) -> bool:
        """True when nothing required failed.

        Checks that were never exercised do not fail the run, but they are
        reported prominently: a reviewer needs to know what was not covered.
        """
        return not self.blocking_failures


class ConformanceHarness:
    """An instrumented platform plus a scenario driver."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        platform_port: int = 0,
        device_port: int = 0,
        workstation: str = "CERTCUPPSCKI001",
        devices: Optional[list[SimulatedDevice]] = None,
        application_name: str = "",
        application_version: str = "",
        platform: Optional[PlatformSimulator] = None,
    ) -> None:
        self.recorder = Recorder()
        # Any platform built on the simulator's protocol handling can be
        # assessed against, including the production engine
        # (cuppsplatform.server.CuppsPlatform), whose peripherals sit behind
        # real drivers.
        self.simulator = platform or PlatformSimulator(
            host=host,
            platform_port=platform_port,
            device_port=device_port,
            workstation=workstation,
            location="CUPPS conformance harness",
            devices=devices if devices is not None else default_devices(workstation),
        )
        self.simulator.recorder = self.recorder
        self.application_name = application_name
        self.application_version = application_version
        self._started_at = ""

    # -- lifecycle --------------------------------------------------------

    def start(self) -> "ConformanceHarness":
        self.simulator.start()
        self._started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.recorder.note("harness started")
        return self

    def stop(self) -> None:
        self.simulator.stop()

    def __enter__(self) -> "ConformanceHarness":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    @property
    def environment(self) -> dict[str, str]:
        """The environment variables the application under test should use."""
        return self.simulator.environment_overrides

    def describe_environment(self) -> str:
        """A copy-pasteable block for whoever is running the test."""
        lines = [
            "Point the application under test at this harness:",
            "",
        ]
        for key, value in sorted(self.environment.items()):
            lines.append(f"  export {key}={value}")
        return "\n".join(lines)

    # -- waiting ----------------------------------------------------------

    def wait_for_application(self, timeout: float = 120.0) -> bool:
        """Block until an application authenticates, or the timeout expires."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.recorder.issued_tokens:
                return True
            time.sleep(0.2)
        return False

    def wait_for_devices(self, count: int = 1, timeout: float = 60.0) -> bool:
        """Block until the application has acquired ``count`` devices."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            acquired = {
                connection.device_name
                for connection in self.recorder.device_connections()
                if connection.device_name
            }
            if len(acquired) >= count:
                return True
            time.sleep(0.2)
        return False

    # -- scenario steps ---------------------------------------------------

    def _acquired_of_type(self, device_type: str) -> Optional[str]:
        """The name of an acquired device of ``device_type``, if any."""
        for connection in self.recorder.device_connections():
            name = connection.device_name
            if not name:
                continue
            device = self.simulator.device(name)
            if device is not None and device.device_type.upper() == device_type:
                return name
        return None

    def step_scan_boarding_pass(self) -> None:
        name = self._acquired_of_type("BC")
        if name is None:
            self.recorder.note("skipped scan: no BC device acquired")
            return
        self.recorder.note(f"injected a boarding pass scan on {name}")
        self.simulator.scan_barcode(name, SAMPLE_BOARDING_PASS)

    def step_swipe_card(self) -> None:
        name = self._acquired_of_type("MS")
        if name is None:
            self.recorder.note("skipped swipe: no MS device acquired")
            return
        self.recorder.note(f"injected a card swipe on {name}")
        try:
            self.simulator.swipe_card(name, {1: "CONFORMANCE TEST", 2: "0000000000"})
        except RuntimeError as exc:
            self.recorder.note(f"swipe could not be injected: {exc}")

    def step_printer_fault(self) -> None:
        name = self._acquired_of_type("PR") or self._acquired_of_type("BP")
        if name is None:
            self.recorder.note("skipped printer fault: no printer acquired")
            return
        self.recorder.note(f"set {name} to paperOut")
        self.simulator.set_status(name, paper_out=True)

    def step_printer_recovered(self) -> None:
        name = self._acquired_of_type("PR") or self._acquired_of_type("BP")
        if name is None:
            return
        self.recorder.note(f"cleared paperOut on {name}")
        self.simulator.set_status(name, paper_out=False)

    def step_device_powered_off(self) -> None:
        name = self._acquired_of_type("BC")
        if name is None:
            return
        self.recorder.note(f"set {name} to powerOff")
        self.simulator.set_status(name, power_off=True, ready=False)
        time.sleep(1.5)
        self.recorder.note(f"restored {name}")
        self.simulator.set_status(name, power_off=False, ready=True)

    def step_directed_stop(self) -> None:
        told = self.simulator.request_application_stop()
        self.recorder.note(
            f"sent <applicationStopCommandRequest> to {told} session(s)"
        )

    def _platform_peers(self) -> list:
        with self.simulator._peers_lock:  # noqa: SLF001 - harness owns the sim
            return [
                peer
                for peer, state in self.simulator._peers.items()  # noqa: SLF001
                if state.get("token") and state.get("device") is None
            ]

    def step_drop_platform_session(self) -> None:
        """Drop the platform connection cleanly (section 17.1.2, 26.7).

        Compliance testing must include "full device session recovery upon
        loss of the platform session", so this is the one step a conformance
        run should never skip.

        ``shutdown`` before ``close`` is what actually sends the FIN: closing
        a socket that another thread is blocked reading leaves the connection
        half-open instead, which is a different scenario entirely -- see
        :meth:`step_stall_platform_session`.
        """
        import socket as socket_module

        closed = 0
        killed: list[int] = []
        for peer in self._platform_peers():
            try:
                peer.sock.shutdown(socket_module.SHUT_RDWR)
            except OSError:
                pass
            try:
                peer.sock.close()
            except OSError:
                pass
            peer.closed = True
            killed.append(peer.connection_id)
            closed += 1
        self.recorder.mark_harness_closed(killed)
        self.recorder.invalidate_all_tokens()
        self.recorder.note(
            f"platform session dropped ({closed} connection(s) closed "
            f"cleanly); device tokens are now invalid"
        )

    # -- scenario driver --------------------------------------------------

    def default_scenario(self) -> list[ScenarioStep]:
        """The standard conformance scenario."""
        return [
            ScenarioStep(
                "scan", "Inject a boarding pass scan on a BC device",
                lambda h: h.step_scan_boarding_pass(),
            ),
            ScenarioStep(
                "swipe", "Inject a magnetic stripe read on an MS device",
                lambda h: h.step_swipe_card(),
            ),
            ScenarioStep(
                "printer-fault", "Take a printer out of paper",
                lambda h: h.step_printer_fault(),
            ),
            ScenarioStep(
                "printer-recovered", "Restore the printer",
                lambda h: h.step_printer_recovered(),
            ),
            ScenarioStep(
                "device-offline", "Power a reader off and back on",
                lambda h: h.step_device_powered_off(), settle=3.0,
            ),
            ScenarioStep(
                "platform-loss",
                "Drop the platform session cleanly and require full device "
                "session recovery (section 17.1.2)",
                lambda h: h.step_drop_platform_session(),
                settle=20.0,
            ),
            ScenarioStep(
                "directed-stop",
                "Direct the application to stop and observe its closing "
                "handshake (section 29.2)",
                lambda h: h.step_directed_stop(),
                settle=12.0,
            ),
        ]

    def run_scenario(
        self,
        steps: Optional[list[ScenarioStep]] = None,
        *,
        on_step: Optional[Callable[[ScenarioStep], None]] = None,
    ) -> list[str]:
        """Drive the scenario, returning the names of the steps run."""
        steps = steps if steps is not None else self.default_scenario()
        run: list[str] = []
        for step in steps:
            if on_step:
                on_step(step)
            log.info("scenario step: %s", step.name)
            step.run(self)
            time.sleep(step.settle)
            run.append(step.name)
        return run

    # -- evaluation -------------------------------------------------------

    def evaluate(self, steps_run: Optional[list[str]] = None) -> RunResult:
        """Run the check catalogue against what was recorded."""
        findings = run_all(self.recorder)
        return RunResult(
            started=self._started_at,
            finished=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            duration=self.recorder.duration,
            findings=findings,
            recorder=self.recorder,
            steps_run=steps_run or [],
            application_name=self.application_name,
            application_version=self.application_version,
        )
