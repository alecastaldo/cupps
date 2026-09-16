#!/usr/bin/env python3
"""Generate CUPPS application release notes (TS 01.04.0004 section 17.3.1).

Release notes for a CUPPS application are not freeform: section 17.3.1
enumerates the elements they "shall contain", and a platform supplier can
reject a hand-over that is missing any of them.  Across a dozen platform
suppliers and a few hundred sites, writing them by hand is both toil and a
recurring source of rejected hand-overs.

Almost every field is already knowable from the build: fixed and added items
from the commit log, targeted specification versions from the interface
library, dependencies from the project metadata, and -- the field that matters
most operationally -- whether the CUPPS interface moved, which decides whether
this release needs re-certification at all (section 17.1.2).

    python3 tools/release_notes.py --since v1.0.0 \\
        --airline "EXAMPLE AIRLINES" \\
        --contact-email ops@example.com --contact-phone "+44 20 0000 0000" \\
        --contact-hours "0700-1900 GMT+0" \\
        --sites LHR JFK FRA --beta-site LHR \\
        --activation 2026-10-01T02:00Z
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

#: Conventional prefixes mapped to the three lists section 17.3.1 requires.
_ADDED = re.compile(r"^(add|feat|introduce|implement|support)\b", re.I)
_REMOVED = re.compile(r"^(remove|drop|delete|retire|obsolete)\b", re.I)


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _commits(since: Optional[str]) -> list[str]:
    span = f"{since}..HEAD" if since else "HEAD"
    output = _git("log", "--no-merges", "--pretty=format:%s", span)
    return [line.strip() for line in output.splitlines() if line.strip()]


def _classify(commits: list[str]) -> dict[str, list[str]]:
    """Split commit subjects into the added / fixed / removed lists."""
    buckets: dict[str, list[str]] = {"added": [], "fixed": [], "removed": []}
    for subject in commits:
        if _REMOVED.match(subject):
            buckets["removed"].append(subject)
        elif _ADDED.match(subject):
            buckets["added"].append(subject)
        else:
            # Section 17.3.1 wants items "fixed"; anything that is neither an
            # addition nor a removal is reported there rather than dropped.
            buckets["fixed"].append(subject)
    return buckets


def _interface_status() -> tuple[str, str]:
    """Whether the CUPPS interface moved since the recorded baseline."""
    from tools.interface_gate import interface_surface, load_baseline, signature

    baseline = load_baseline()
    current = signature(interface_surface())
    recorded = baseline.get("signature", "")
    if not recorded:
        return current, "no baseline recorded"
    if recorded == current:
        return current, "unchanged"
    return current, f"CHANGED from {recorded}"


def _dependencies() -> list[str]:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.9/3.10
        return []
    pyproject = REPO_ROOT / "pyproject.toml"
    if not pyproject.exists():
        return []
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return list(data.get("project", {}).get("dependencies", []))


def _device_requirements() -> tuple[list[str], list[str]]:
    """Devices the application requires, and those it additionally supports."""
    from cuppsd.service import DEFAULT_DEVICE_MODES

    # The "defined minimum" a check-in and boarding application cannot work
    # without; everything else it will use if the platform offers it.
    minimum = ["BC", "PR"]
    additional = [d for d in sorted(DEFAULT_DEVICE_MODES) if d not in minimum]
    return minimum, additional


def build(args: argparse.Namespace) -> dict:
    from cupps import CUPPS_TS_VERSION, SUPPORTED_INTERFACE_LEVELS
    from cuppsd import __version__ as app_version

    commits = _commits(args.since)
    buckets = _classify(commits)
    signature, interface_state = _interface_status()
    minimum, additional = _device_requirements()

    return {
        "airline": args.airline,
        "applicationName": args.application_name,
        "applicationType": args.application_type,
        "version": args.version or app_version,
        "releaseDate": args.release_date or date.today().isoformat(),
        "contact": {
            "phone": args.contact_phone,
            "email": args.contact_email,
            "hours": args.contact_hours,
        },
        "targetSites": args.sites,
        "siteSubsection": args.site_subsection,
        "betaSites": args.beta_site,
        "activation": args.activation,
        "activationCritical": args.activation_critical,
        "specificationVersions": list(SUPPORTED_INTERFACE_LEVELS),
        "specificationIssue": CUPPS_TS_VERSION,
        "interfaceSignature": signature,
        "interfaceState": interface_state,
        "recertificationRequired": interface_state.startswith("CHANGED"),
        "fixed": buckets["fixed"],
        "added": buckets["added"],
        "removed": buckets["removed"],
        "description": args.description,
        "incompatibilities": args.incompatibility,
        "installationInstructions": args.install_note,
        "testedOn": args.tested_on,
        "knownBadOn": args.known_bad_on,
        "replaces": args.replaces,
        "deviceRequirements": minimum,
        "additionalDevices": additional,
        "dependencies": _dependencies(),
    }


def render(notes: dict) -> str:
    def block(title: str, items: list[str]) -> list[str]:
        # Section 17.3.1: "If the release does not ... then the release notes
        # shall specify None."
        lines = [title]
        for item in items or ["None"]:
            lines.append(f"  - {item}")
        lines.append("")
        return lines

    contact = notes["contact"]
    lines: list[str] = [
        "=" * 74,
        f"  {notes['applicationName']} {notes['version']} - RELEASE NOTES",
        "=" * 74,
        "",
        f"Airline / company ....... {notes['airline'] or '(not stated)'}",
        f"Application name ........ {notes['applicationName']}",
        f"Application type ........ {notes['applicationType']}",
        f"Version ................. {notes['version']}",
        f"Release date ............ {notes['releaseDate']}",
        "",
        "Technical contact",
        f"  Phone ................. {contact['phone'] or '(not stated)'}",
        f"  Email ................. {contact['email'] or '(not stated)'}",
        f"  Best hours ............ {contact['hours'] or '(not stated)'}",
        "",
        f"Target sites ............ {', '.join(notes['targetSites']) or 'All'}",
    ]
    if notes["siteSubsection"]:
        lines.append(f"  Sub-section ........... {notes['siteSubsection']}")
    lines += [
        f"Preferred beta site(s) .. {', '.join(notes['betaSites']) or '(not stated)'}",
        f"Activation .............. {notes['activation'] or '(to be agreed)'}"
        + ("   ** CRITICAL DATE **" if notes["activationCritical"] else ""),
        "",
        f"CUPPS TS versions ....... {', '.join(notes['specificationVersions'])}",
        f"Written against ......... {notes['specificationIssue']}",
        f"Replaces ................ {notes['replaces'] or '(new application)'}",
        "",
        "-" * 74,
        "CUPPS INTERFACE",
        "-" * 74,
        f"Interface signature ..... {notes['interfaceSignature']}",
        f"Interface state ......... {notes['interfaceState']}",
        "",
    ]
    if notes["recertificationRequired"]:
        lines += [
            "  ** This release CHANGES the software interface to the platform.",
            "  ** Section 17.1.2 requires Application Compliance Testing to be",
            "  ** repeated and every platform integration to be revalidated.",
            "",
        ]
    else:
        lines += [
            "  The CUPPS interface is unchanged in this release, so under",
            "  section 17.1.2 it does not require Application Compliance",
            "  Testing to be repeated.",
            "",
        ]

    lines += ["-" * 74, "CHANGES", "-" * 74, ""]
    lines += block("Fixed in this release:", notes["fixed"])
    lines += block("Added in this release:", notes["added"])
    lines += block("Removed in this release:", notes["removed"])

    if notes["description"]:
        lines += ["Description:", ""]
        lines += [f"  {line}" for line in notes["description"].splitlines()]
        lines.append("")

    lines += ["-" * 74, "ENVIRONMENT", "-" * 74, ""]
    lines += block("Tested on:", notes["testedOn"])
    lines += block("Known not to work on:", notes["knownBadOn"])
    lines += block("Known incompatibilities:", notes["incompatibilities"])
    lines += block("Required devices:", notes["deviceRequirements"])
    lines += block("Additional devices supported:", notes["additionalDevices"])
    lines += block("Third party dependencies:", notes["dependencies"])
    lines += block("Installation instructions:", notes["installationInstructions"])

    lines += [
        "=" * 74,
        "Generated from the build. Fields follow CUPPS TS 01.04.0004 section",
        "17.3.1; review before hand-over to the platform supplier.",
        "=" * 74,
    ]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="release_notes",
        description="Generate CUPPS application release notes (section 17.3.1).",
    )
    parser.add_argument("--since", help="git ref to diff from, e.g. a release tag")
    parser.add_argument("--airline", default="", help="airline or company name")
    parser.add_argument("--application-name", default="CUPPSAGENT")
    parser.add_argument("--application-type", default="check-in and boarding")
    parser.add_argument("--version", default="")
    parser.add_argument("--release-date", default="",
                        help="ISO 8601; defaults to today")
    parser.add_argument("--contact-phone", default="")
    parser.add_argument("--contact-email", default="")
    parser.add_argument("--contact-hours", default="",
                        help="best hours to be reached, with GMT offset")
    parser.add_argument("--sites", nargs="*", default=[],
                        help="target sites as 3-letter IATA codes")
    parser.add_argument("--site-subsection", default="",
                        help="e.g. a specific terminal")
    parser.add_argument("--beta-site", nargs="*", default=[])
    parser.add_argument("--activation", default="",
                        help="target production activation date and time")
    parser.add_argument("--activation-critical", action="store_true",
                        help="flag the activation date as critical, e.g. a "
                             "government mandated deadline")
    parser.add_argument("--description", default="")
    parser.add_argument("--incompatibility", nargs="*", default=[])
    parser.add_argument("--install-note", nargs="*", default=[])
    parser.add_argument("--tested-on", nargs="*",
                        default=["Windows 10 Professional 64-bit"])
    parser.add_argument("--known-bad-on", nargs="*", default=[])
    parser.add_argument("--replaces", default="")
    parser.add_argument("--json", action="store_true", help="emit JSON instead")
    parser.add_argument("--out", default="", help="write to this file as well")
    args = parser.parse_args(argv)

    notes = build(args)
    output = json.dumps(notes, indent=2) if args.json else render(notes)
    print(output)
    if args.out:
        Path(args.out).write_text(output + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
