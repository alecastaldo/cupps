#!/usr/bin/env python3
"""Inspect and validate the device profile catalogue.

    python3 tools/profiles.py --list
    python3 tools/profiles.py --validate
    python3 tools/profiles.py --match PR "Boca Systems" "Lemur-2"
    python3 tools/profiles.py --explain LHRT4LB00302PR1 --simulator

``--validate`` is what CI runs: a malformed profile should fail a build, not a
gate at six in the morning.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cuppsd.deviceprofile import (  # noqa: E402
    PROFILE_DIR,
    ProfileError,
    ProfileRegistry,
)


@dataclass
class _FakeDevice:
    """Enough of a device for matching, for offline reasoning."""

    device_type: str
    vendor: str = ""
    model: str = ""
    misc_info: str = ""
    name: str = "(hypothetical)"


def list_profiles(registry: ProfileRegistry) -> int:
    print()
    print(f"{'PROFILE':<26}{'TYPE':<6}{'VERIFIED':<10}DESCRIPTION")
    print("-" * 100)
    for profile in registry:
        mark = "yes" if profile.verified else "NO"
        print(
            f"{profile.profile_id:<26}{profile.device_type:<6}{mark:<10}"
            f"{profile.description[:56]}"
        )

    unverified = registry.unverified
    print()
    print(f"{len(registry)} profile(s); {len(unverified)} unverified.")
    if unverified:
        print()
        print("  Unverified means nobody has run the device test against real")
        print("  hardware. These are datasheet guesses until someone has put")
        print("  paper through the printer:")
        for profile in unverified:
            print(f"    - {profile.profile_id}")
    print()
    return 0


def validate(registry: ProfileRegistry, directories: list[Path]) -> int:
    """Re-load strictly and report anything a build should reject."""
    problems: list[str] = []

    if len(registry) == 0:
        problems.append("no profiles were loaded at all")

    seen_matches: dict[tuple, list[str]] = {}
    for profile in registry:
        if not profile.description:
            problems.append(f"{profile.profile_id}: no description")
        if not profile.verified and "TEMPLATE" not in profile.notes.upper():
            problems.append(
                f"{profile.profile_id}: unverified profiles must say so in "
                f"notes, so nobody mistakes a datasheet guess for a tested "
                f"device"
            )
        key = (
            profile.device_type,
            profile.match.vendor.pattern if profile.match.vendor else None,
            profile.match.model.pattern if profile.match.model else None,
            profile.match.misc_info.pattern if profile.match.misc_info else None,
        )
        seen_matches.setdefault(key, []).append(profile.profile_id)

    for key, ids in seen_matches.items():
        if len(ids) > 1:
            problems.append(
                f"profiles {', '.join(sorted(ids))} have identical match "
                f"criteria for {key[0]}; which one wins would depend on the "
                f"profile id alone"
            )

    print()
    if problems:
        print(f"{len(problems)} problem(s) in the profile catalogue:")
        for problem in problems:
            print(f"  - {problem}")
        print()
        return 1

    verified = sum(1 for p in registry if p.verified)
    print(
        f"Profile catalogue is valid: {len(registry)} profile(s), "
        f"{verified} verified."
    )
    for directory in directories:
        print(f"  searched {directory}")
    print()
    return 0


def explain_match(registry: ProfileRegistry, device) -> int:
    profile = registry.resolve(device)
    print()
    print(f"Device ....... {device.name}")
    print(f"  type ....... {device.device_type}")
    print(f"  vendor ..... {device.vendor or '(none reported)'}")
    print(f"  model ...... {device.model or '(none reported)'}")
    print(f"  miscInfo ... {device.misc_info or '(none reported)'}")
    print()
    print(f"Profile ...... {profile.profile_id}")
    print(f"  {profile.description}")
    print(f"  verified ... {'yes' if profile.verified else 'NO'}")
    if profile.source:
        print(f"  source ..... {profile.source}")
    print()
    print("Behaviour applied:")
    print(f"  paper jam reported independently .. "
          f"{profile.status.reports_paper_jam_independently}")
    print(f"  error survives a power cycle ...... "
          f"{profile.status.retains_error_across_power_cycle}")
    print(f"  print timeout ..................... "
          f"{profile.timing.print_timeout_seconds}s")
    if profile.aea.opening_commands:
        print(f"  AEA opening commands (after EP) ... "
              f"{list(profile.aea.opening_commands)}")
    if profile.aea.templates:
        print(f"  AEA templates ..................... "
              f"{sorted(profile.aea.templates)}")
    if profile.stocks.aliases:
        print(f"  stock aliases ..................... {profile.stocks.aliases}")
    if profile.notes:
        print()
        print("Notes:")
        for line in profile.notes.split(". "):
            if line.strip():
                print(f"  {line.strip().rstrip('.')}.")
    print()

    others = [
        p.profile_id
        for p in registry.for_type(device.device_type)
        if p.profile_id != profile.profile_id
    ]
    if others:
        print(f"Other {device.device_type} profiles that did not match: "
              f"{', '.join(others)}")
        print()
    return 0


def explain_live(registry: ProfileRegistry, device_name: str,
                 use_simulator: bool) -> int:
    """Resolve the profile for a device on a live platform."""
    from cupps import CuppsEnvironment, PlatformSession

    simulator = None
    overrides = {}
    if use_simulator:
        from simulator import PlatformSimulator

        simulator = PlatformSimulator().start()
        overrides = simulator.environment_overrides

    try:
        environment = CuppsEnvironment.from_os(overrides)
        with PlatformSession(
            environment.platform_node,
            environment.platform_port,
            airline=environment.airline or "ZZ",
            event_token="PROFILETOOL00001",
        ) as platform:
            device = platform.environment.by_name(device_name)
            if device is None:
                available = [d.name for d in platform.environment.all_devices()]
                print(
                    f"\nNo device named {device_name!r} on this platform.\n"
                    f"Available: {', '.join(available)}\n",
                    file=sys.stderr,
                )
                return 1
            return explain_match(registry, device)
    finally:
        if simulator:
            simulator.stop()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="profiles",
        description="Inspect and validate the CUPPS device profile catalogue.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true")
    group.add_argument("--validate", action="store_true")
    group.add_argument(
        "--match", nargs="+", metavar=("TYPE", "VENDOR"),
        help="resolve a hypothetical device: TYPE [VENDOR [MODEL [MISCINFO]]]",
    )
    group.add_argument(
        "--explain", metavar="DEVICENAME",
        help="resolve the profile for a device on a live platform",
    )
    parser.add_argument(
        "--profile-dir", action="append", default=[], type=Path,
        help="an additional profile directory, searched after the shipped one",
    )
    parser.add_argument(
        "--simulator", action="store_true",
        help="with --explain, use the bundled platform simulator",
    )
    args = parser.parse_args(argv)

    try:
        registry = ProfileRegistry.load(*args.profile_dir)
    except ProfileError as exc:
        print(f"\nProfile catalogue is invalid:\n  {exc}\n", file=sys.stderr)
        return 1

    if args.list:
        return list_profiles(registry)
    if args.validate:
        return validate(registry, [PROFILE_DIR, *args.profile_dir])
    if args.match:
        fields = list(args.match) + [""] * (4 - len(args.match))
        device = _FakeDevice(
            device_type=fields[0].upper(),
            vendor=fields[1],
            model=fields[2],
            misc_info=fields[3],
            name=f"(hypothetical {fields[0].upper()})",
        )
        return explain_match(registry, device)
    return explain_live(registry, args.explain, args.simulator)


if __name__ == "__main__":
    raise SystemExit(main())
