#!/usr/bin/env python3
"""Detect changes to the CUPPS interface layer, which force re-certification.

Section 17.1.2 lists what requires Application Compliance Testing to be
repeated.  Three of the four triggers are occasional and obvious -- a new
application, a new operating system, falling more than two major versions
behind.  The fourth is the one that fires by accident:

    "Any modification to a CUPPS Compliant Application which changes the
     software interface to the CUPPS Compliant Platform."

Business logic, the agent UI, DCS adapters and document layouts can all change
freely without re-certification.  The wire-facing layer cannot.  At a few
hundred releases a year across a dozen platform suppliers, noticing that
distinction by eye does not scale, and getting it wrong is expensive in both
directions: an unnoticed change ships uncertified, and a wrongly assumed one
buys a certification cycle nobody needed.

This tool computes a signature of the public surface of ``cupps/`` and fails
the build when it moves without being declared.

    python3 tools/interface_gate.py --check        # CI gate
    python3 tools/interface_gate.py --update       # accept a declared change
    python3 tools/interface_gate.py --show         # print the current surface

The signature deliberately covers the *interface* surface -- module, class and
function names, signatures, and the protocol constants -- not implementation.
Refactoring the body of a method does not trip the gate; changing what goes on
the wire does.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
INTERFACE_PACKAGE = REPO_ROOT / "cupps"
BASELINE = REPO_ROOT / "cupps" / "interface-signature.json"

#: Modules whose *constants* are part of the wire contract, so a changed value
#: counts as an interface change even though no name moved.
VALUE_SENSITIVE = {"params.py", "header.py", "results.py"}


def _public(name: str) -> bool:
    return not name.startswith("_")


def _signature_of(node: ast.AST) -> str:
    """A stable textual form of a function or method signature."""
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    args = node.args
    parts: list[str] = []
    for argument in [*args.posonlyargs, *args.args]:
        parts.append(argument.arg)
    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    if args.kwonlyargs:
        if not args.vararg:
            parts.append("*")
        parts.extend(argument.arg for argument in args.kwonlyargs)
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")
    return f"{node.name}({', '.join(parts)})"


def _constant_value(node: ast.AST) -> str:
    """Render a constant expression, or a marker when it is computed."""
    try:
        return repr(ast.literal_eval(node))
    except (ValueError, TypeError, SyntaxError):
        return "<computed>"


def surface_of(path: Path) -> list[str]:
    """The public interface surface of one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module = path.name
    entries: list[str] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _public(node.name):
                entries.append(f"{module}::def {_signature_of(node)}")

        elif isinstance(node, ast.ClassDef):
            if not _public(node.name):
                continue
            bases = ", ".join(ast.unparse(base) for base in node.bases)
            entries.append(f"{module}::class {node.name}({bases})")
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if _public(member.name):
                        entries.append(
                            f"{module}::{node.name}.{_signature_of(member)}"
                        )
                elif isinstance(member, ast.AnnAssign) and isinstance(
                    member.target, ast.Name
                ):
                    if _public(member.target.id):
                        entries.append(
                            f"{module}::{node.name}.{member.target.id}: "
                            f"{ast.unparse(member.annotation)}"
                        )

        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (
                [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            )
            for target in targets:
                if not isinstance(target, ast.Name) or not _public(target.id):
                    continue
                if module in VALUE_SENSITIVE and node.value is not None:
                    entries.append(
                        f"{module}::{target.id} = {_constant_value(node.value)}"
                    )
                else:
                    entries.append(f"{module}::{target.id}")

    return sorted(entries)


def interface_surface() -> list[str]:
    """The whole public surface of the interface package."""
    entries: list[str] = []
    for path in sorted(INTERFACE_PACKAGE.glob("*.py")):
        entries.extend(surface_of(path))
    return entries


def signature(entries: list[str]) -> str:
    digest = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
    return digest[:32].upper()


def load_baseline() -> dict:
    if not BASELINE.exists():
        return {}
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def write_baseline(entries: list[str], note: str) -> dict:
    from cupps import __version__ as interface_version

    payload = {
        "_comment": (
            "Signature of the CUPPS interface surface. A change here means "
            "section 17.1.2 requires Application Compliance Testing to be "
            "repeated. Regenerate with tools/interface_gate.py --update and "
            "say why in the release notes."
        ),
        "interfaceVersion": interface_version,
        "signature": signature(entries),
        "entryCount": len(entries),
        "note": note,
        "entries": entries,
    }
    BASELINE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def _diff(old: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    old_set, new_set = set(old), set(new)
    return sorted(new_set - old_set), sorted(old_set - new_set)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="interface_gate",
        description="Fail the build when the CUPPS interface surface changes "
                    "without being declared (TS 01.04.0004 section 17.1.2).",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true",
                       help="compare against the recorded baseline")
    group.add_argument("--update", action="store_true",
                       help="record the current surface as the new baseline")
    group.add_argument("--show", action="store_true",
                       help="print the current interface surface")
    parser.add_argument("--note", default="",
                        help="with --update, why the interface changed")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(REPO_ROOT))
    entries = interface_surface()
    current = signature(entries)

    if args.show:
        for entry in entries:
            print(entry)
        print(f"\n{len(entries)} public interface entries, signature {current}")
        return 0

    if args.update:
        if not args.note:
            print(
                "interface_gate: --update needs --note explaining what changed "
                "on the wire, because it goes in the release notes and the "
                "re-certification request.",
                file=sys.stderr,
            )
            return 2
        payload = write_baseline(entries, args.note)
        print(f"Baseline updated: {payload['signature']} "
              f"({payload['entryCount']} entries)")
        print(f"  note: {args.note}")
        print(
            "\n  This is an interface change. Section 17.1.2 requires "
            "Application\n  Compliance Testing to be repeated, and every "
            "platform supplier\n  integration to be revalidated."
        )
        return 0

    baseline = load_baseline()
    if not baseline:
        print(
            "interface_gate: no baseline recorded. Create one with:\n"
            "  python3 tools/interface_gate.py --update --note 'initial baseline'",
            file=sys.stderr,
        )
        return 2

    recorded = baseline.get("signature", "")
    if recorded == current:
        print(f"Interface unchanged: {current} ({len(entries)} entries)")
        print("  This release does not require re-certification under "
              "section 17.1.2.")
        return 0

    added, removed = _diff(baseline.get("entries", []), entries)
    print("INTERFACE CHANGED", file=sys.stderr)
    print(f"  baseline {recorded}", file=sys.stderr)
    print(f"  current  {current}", file=sys.stderr)
    print("", file=sys.stderr)
    for entry in added:
        print(f"  + {entry}", file=sys.stderr)
    for entry in removed:
        print(f"  - {entry}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "  Section 17.1.2: any modification which changes the software "
        "interface to\n  the platform requires Application Compliance Testing "
        "to be repeated, and\n  every platform supplier integration to be "
        "revalidated.\n\n"
        "  If that is intended, record it:\n"
        "    python3 tools/interface_gate.py --update --note '<what changed>'\n\n"
        "  If it is not, the change belongs in cuppsd/ rather than cupps/.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
