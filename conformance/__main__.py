"""``cupps-certify`` -- run a self-service Application Compliance assessment.

Two ways to use it.

**Against your own application, launched by the harness:**

    python -m conformance --launch "python3 -m cuppsd" --name MYAPP --version 01.00

**Against an application you start yourself** (any language, any host):

    python -m conformance --observe
    # prints the environment variables to point your application at,
    # waits for it to connect, drives the scenario, then reports

The record is written as text, JSON and HTML.  Attach the HTML to an
integration or certification request; keep the JSON for a fleet dashboard.
"""

from __future__ import annotations

import argparse
import logging
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conformance import report as report_module  # noqa: E402
from conformance.checks import catalogue  # noqa: E402
from conformance.harness import ConformanceHarness  # noqa: E402

log = logging.getLogger("cupps-certify")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cupps-certify",
        description=(
            "Self-service CUPPS Application conformance assessment. Produces a "
            "Compliance Testing Record to submit for formal testing; it does "
            "not itself certify anything (TS 01.04.0004 section 17.1.2)."
        ),
    )
    parser.add_argument(
        "--launch", metavar="COMMAND",
        help="command that starts the application under test; the harness "
             "sets the CUPPS environment variables for it",
    )
    parser.add_argument(
        "--observe", action="store_true",
        help="print the environment and wait for an application you start "
             "yourself to connect",
    )
    parser.add_argument("--name", default="", help="application name for the record")
    parser.add_argument("--version", default="", help="application version")
    parser.add_argument(
        "--out", default="conformance-record",
        help="output path prefix; .txt, .json and .html are written",
    )
    parser.add_argument(
        "--wait", type=float, default=90.0,
        help="seconds to wait for the application to authenticate (default 90)",
    )
    parser.add_argument(
        "--devices", type=int, default=1,
        help="devices the application must acquire before the scenario starts",
    )
    parser.add_argument(
        "--skip-recovery", action="store_true",
        help="skip the platform-loss step. Section 17.1.2 requires compliance "
             "testing to cover device session recovery, so a record produced "
             "with this flag is incomplete and says so.",
    )
    parser.add_argument(
        "--list-checks", action="store_true",
        help="print the check catalogue and exit",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--platform-port", type=int, default=0)
    parser.add_argument("--device-port", type=int, default=0)
    parser.add_argument("--verbose", action="store_true",
                        help="include informational checks and all evidence")
    parser.add_argument("--log-level", default="WARNING",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def list_checks() -> int:
    print()
    print(f"{'CHECK':<11}{'SEVERITY':<15}{'SECTION':<22}TITLE")
    print("-" * 100)
    for check in catalogue():
        print(
            f"{check.check_id:<11}{check.severity:<15}"
            f"{check.section:<22}{check.title}"
        )
    print()
    print(f"{len(catalogue())} checks.")
    print(
        "\nOfficial CUPPS Application Test Cases (CUPPS-ATC-01.04.*) are "
        "published on\nthe IATA MS Teams site and carry their own case "
        "numbers; map them into\nconformance/checks.py so this record lines "
        "up with what a CTE works from."
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)-7s %(name)s  %(message)s",
    )

    if args.list_checks:
        return list_checks()

    if not args.launch and not args.observe:
        print(
            "cupps-certify: choose --launch \"<command>\" or --observe.\n"
            "Run with --help for details, or --list-checks to see what is "
            "assessed.",
            file=sys.stderr,
        )
        return 2

    harness = ConformanceHarness(
        host=args.host,
        platform_port=args.platform_port,
        device_port=args.device_port,
        application_name=args.name,
        application_version=args.version,
    ).start()

    child: Optional[subprocess.Popen] = None
    try:
        print()
        print("  CUPPS conformance harness")
        print(f"  platform {harness.simulator.host}:{harness.simulator.platform_port}"
              f"   devices {harness.simulator.host}:{harness.simulator.device_port}")
        print()

        if args.launch:
            environment = dict(os.environ)
            environment.update(harness.environment)
            print(f"  launching: {args.launch}")
            child = subprocess.Popen(
                shlex.split(args.launch),
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            print(harness.describe_environment())
            print()
            print("  Waiting for the application to connect...")

        if not harness.wait_for_application(timeout=args.wait):
            print(
                f"\n  No application authenticated within {args.wait:.0f}s.\n"
                f"  Check that it is pointed at the harness and that it sends\n"
                f"  <authenticateRequest> after the interface level handshake.",
                file=sys.stderr,
            )
            return _finish(harness, args, [], exit_code=3)

        print("  Application connected. Waiting for devices...")
        harness.wait_for_devices(count=args.devices, timeout=30.0)

        steps = harness.default_scenario()
        if args.skip_recovery:
            steps = [step for step in steps if step.name != "platform-loss"]

        print()
        steps_run = harness.run_scenario(
            steps,
            on_step=lambda step: print(f"  -> {step.name}: {step.description}"),
        )
        print()
        return _finish(harness, args, steps_run)
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        harness.stop()


def _finish(harness, args, steps_run, exit_code: Optional[int] = None) -> int:
    result = harness.evaluate(steps_run)
    text = report_module.to_text(result, verbose=args.verbose)
    print(text)

    prefix = Path(args.out)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    written = []
    for suffix, payload in (
        (".txt", text),
        (".json", report_module.to_json(result)),
        (".html", report_module.to_html(result)),
    ):
        target = prefix.with_suffix(suffix)
        target.write_text(payload, encoding="utf-8")
        written.append(str(target))

    print()
    print("  Record written:")
    for path in written:
        print(f"    {path}")
    print()

    if exit_code is not None:
        return exit_code
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
