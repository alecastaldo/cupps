"""Compliance Testing Record generation.

Emits the record in three forms:

* **text**  -- for a terminal and for CI output
* **JSON**  -- machine readable, for a dashboard across many applications
* **HTML**  -- the document to attach to an integration or certification
  request, self-contained with no external assets

Every form carries the same prominent statement of what the record is and is
not, because section 17.1.2 reserves Application Compliance Testing to a
compliant platform supplier or the IATA approved CTE.  A report that let
someone believe otherwise would be worse than no report at all.
"""

from __future__ import annotations

import hashlib
import json
import platform as platform_module
from datetime import datetime, timezone
from typing import Any

from cupps import CUPPS_TS_VERSION
from cupps import __version__ as library_version

from .checks import FAIL, INFO, INFORMATIONAL, NOT_EXERCISED, PASS, REQUIRED
from .harness import RunResult

#: Shown on every report, in every format.
DISCLAIMER = (
    "This is a self-assessment record, not a certificate. Under CUPPS TS "
    "01.04.0004 section 17.1.2, Application Compliance Testing is performed "
    "by a compliant platform supplier or by the IATA approved Compliance "
    "Testing Entity. This record is evidence to submit to them."
)

_OUTCOME_LABEL = {
    PASS: "PASS",
    FAIL: "FAIL",
    NOT_EXERCISED: "NOT EXERCISED",
    INFO: "INFO",
}


def summarise(result: RunResult) -> dict[str, int]:
    counts = {"pass": 0, "fail": 0, "not-exercised": 0, "info": 0}
    for finding in result.findings:
        counts[finding.outcome] = counts.get(finding.outcome, 0) + 1
    return counts


def fingerprint(result: RunResult) -> str:
    """A stable digest of the outcome, so a record can be referenced.

    Covers the check identities and their outcomes -- not timings -- so the
    same conversation produces the same fingerprint on a re-run.
    """
    material = "|".join(
        f"{finding.check_id}={finding.outcome}"
        for finding in sorted(result.findings, key=lambda f: f.check_id)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16].upper()


def to_dict(result: RunResult) -> dict[str, Any]:
    """The machine-readable record."""
    return {
        "recordType": "CUPPS Application Self-Assessment Record",
        "disclaimer": DISCLAIMER,
        "specification": CUPPS_TS_VERSION,
        "harnessLibraryVersion": library_version,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fingerprint": fingerprint(result),
        "application": {
            "name": result.application_name or "(not stated)",
            "version": result.application_version or "(not stated)",
        },
        "environment": {
            "python": platform_module.python_version(),
            "os": f"{platform_module.system()} {platform_module.release()}",
            "machine": platform_module.machine(),
        },
        "run": {
            "started": result.started,
            "finished": result.finished,
            "durationSeconds": round(result.duration, 1),
            "scenarioSteps": result.steps_run,
            "connections": len(result.recorder.ordered()),
            "messages": result.recorder.message_count,
        },
        "verdict": {
            "passed": result.passed,
            "blockingFailures": len(result.blocking_failures),
            "advisoryFailures": len(result.advisory_failures),
            "notExercised": len(result.not_exercised),
        },
        "summary": summarise(result),
        "findings": [finding.to_dict() for finding in result.findings],
        "log": [
            {
                "at": round(at, 2),
                "note": note,
            }
            for at, note in result.recorder.notes
        ],
        "conversation": [
            {
                "connection": connection.connection_id,
                "kind": connection.label,
                "interfaceLevel": connection.interface_level,
                "interfaceMode": connection.interface_mode,
                "openedAt": round(connection.opened_at, 2),
                "closedAt": (
                    round(connection.closed_at, 2)
                    if connection.closed_at is not None
                    else None
                ),
                "messages": [
                    {
                        "at": round(message.at, 2),
                        "direction": (
                            "app->plt" if message.is_from_application else "plt->app"
                        ),
                        "messageName": message.message_name,
                        "messageID": message.message_id,
                        "bytes": message.raw_length,
                    }
                    for message in connection.messages
                ],
            }
            for connection in result.recorder.ordered()
        ],
    }


def to_json(result: RunResult, *, indent: int = 2) -> str:
    return json.dumps(to_dict(result), indent=indent)


def to_text(result: RunResult, *, verbose: bool = False) -> str:
    """The terminal form."""
    counts = summarise(result)
    lines: list[str] = []
    rule = "=" * 76

    lines.append(rule)
    lines.append("  CUPPS APPLICATION SELF-ASSESSMENT RECORD")
    lines.append(f"  Specification {CUPPS_TS_VERSION}")
    lines.append(rule)
    lines.append("")
    lines.append(f"  Application ..... {result.application_name or '(not stated)'} "
                 f"{result.application_version}")
    lines.append(f"  Run ............. {result.started} -> {result.finished} "
                 f"({result.duration:.0f}s)")
    lines.append(f"  Conversation .... {len(result.recorder.ordered())} connection(s), "
                 f"{result.recorder.message_count} message(s)")
    if result.steps_run:
        lines.append(f"  Scenario ........ {', '.join(result.steps_run)}")
    lines.append(f"  Fingerprint ..... {fingerprint(result)}")
    lines.append("")

    lines.append("-" * 76)
    for finding in result.findings:
        if finding.severity == INFORMATIONAL and not verbose:
            continue
        label = _OUTCOME_LABEL.get(finding.outcome, finding.outcome.upper())
        marker = "!" if finding.blocking else " "
        lines.append(f"{marker} [{label:^13}] {finding.check_id}  {finding.title}")
        lines.append(f"{'':17} section {finding.section}")
        if finding.detail:
            lines.append(f"{'':17} {finding.detail}")
        if finding.evidence and (verbose or finding.outcome == FAIL):
            for item in finding.evidence[:8]:
                lines.append(f"{'':19} - {item}")
            if len(finding.evidence) > 8:
                lines.append(f"{'':19}   ... and {len(finding.evidence) - 8} more")
        lines.append("")

    lines.append("-" * 76)
    lines.append(
        f"  {counts['pass']} passed   {counts['fail']} failed   "
        f"{counts['not-exercised']} not exercised"
    )
    lines.append("")

    if result.blocking_failures:
        lines.append("  VERDICT: NOT READY for compliance testing")
        lines.append("")
        lines.append("  These are required by the specification and will fail formal")
        lines.append("  Application Compliance Testing:")
        for finding in result.blocking_failures:
            lines.append(f"    - {finding.check_id}  {finding.title} "
                         f"(section {finding.section})")
    else:
        lines.append("  VERDICT: no required check failed")

    if result.advisory_failures:
        lines.append("")
        lines.append("  Advisory (the specification says should, or calls the")
        lines.append("  behaviour a bug):")
        for finding in result.advisory_failures:
            lines.append(f"    - {finding.check_id}  {finding.title}")

    if result.not_exercised:
        lines.append("")
        lines.append("  NOT EXERCISED -- this run proves nothing about these. A")
        lines.append("  reviewer will ask why:")
        for finding in result.not_exercised:
            lines.append(f"    - {finding.check_id}  {finding.title}")

    lines.append("")
    lines.append(rule)
    for line in _wrap(DISCLAIMER, 72):
        lines.append(f"  {line}")
    lines.append(rule)
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def to_html(result: RunResult) -> str:
    """A self-contained document to attach to a certification request."""
    counts = summarise(result)
    verdict_ok = result.passed

    def esc(value: Any) -> str:
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    rows = []
    for finding in result.findings:
        outcome_class = {
            PASS: "ok", FAIL: "bad", NOT_EXERCISED: "warn", INFO: "info",
        }.get(finding.outcome, "info")
        evidence = ""
        if finding.evidence:
            items = "".join(f"<li>{esc(item)}</li>" for item in finding.evidence)
            evidence = f"<ul class='ev'>{items}</ul>"
        rows.append(
            f"<tr class='{outcome_class}'>"
            f"<td class='id'>{esc(finding.check_id)}</td>"
            f"<td><b>{esc(finding.title)}</b>"
            f"<div class='sec'>section {esc(finding.section)} &middot; "
            f"{esc(finding.severity)}</div>"
            f"<div class='det'>{esc(finding.detail)}</div>{evidence}</td>"
            f"<td class='out'>{esc(_OUTCOME_LABEL.get(finding.outcome, ''))}</td>"
            f"</tr>"
        )

    log_rows = "".join(
        f"<tr><td class='t'>+{at:.1f}s</td><td>{esc(note)}</td></tr>"
        for at, note in result.recorder.notes
    )

    conversation_rows = []
    for connection in result.recorder.ordered():
        conversation_rows.append(
            f"<tr class='conn'><td colspan='4'>Connection "
            f"{connection.connection_id} &mdash; {esc(connection.label)}"
            + (f" &middot; level {esc(connection.interface_level)}"
               if connection.interface_level else "")
            + (f" &middot; mode {esc(connection.interface_mode)}"
               if connection.interface_mode else "")
            + "</td></tr>"
        )
        for message in connection.messages:
            arrow = "&rarr;" if message.is_from_application else "&larr;"
            direction = "in" if message.is_from_application else "out"
            conversation_rows.append(
                f"<tr class='msg {direction}'><td class='t'>+{message.at:.2f}s</td>"
                f"<td class='a'>{arrow}</td>"
                f"<td>{esc(message.message_name)}</td>"
                f"<td class='n'>id {message.message_id} &middot; "
                f"{message.raw_length} B</td></tr>"
            )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>CUPPS Application Self-Assessment Record</title>
<style>
:root{{--ink:#16202b;--muted:#5c6f83;--line:#d5dee7;--bg:#f4f7fa;--panel:#fff;
 --ok:#1f7a4d;--bad:#c0392b;--warn:#b8860b;--info:#2f7fe0}}
*{{box-sizing:border-box}}
body{{margin:0;padding:28px;background:var(--bg);color:var(--ink);
 font:14px/1.5 "Segoe UI",system-ui,Arial,sans-serif}}
.wrap{{max-width:1000px;margin:0 auto}}
h1{{font-size:21px;margin:0 0 4px}} h2{{font-size:15px;margin:28px 0 10px}}
.sub{{color:var(--muted);font-size:13px;margin-bottom:18px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:8px;
 padding:18px;margin-bottom:18px}}
.verdict{{font-size:17px;font-weight:700;padding:14px 18px;border-radius:8px;
 margin-bottom:18px}}
.verdict.ok{{background:#e6f4ec;color:var(--ok);border:1px solid #b7dfc8}}
.verdict.bad{{background:#fbeae8;color:var(--bad);border:1px solid #f0c2bc}}
dl{{display:grid;grid-template-columns:auto 1fr;gap:6px 20px;margin:0}}
dt{{color:var(--muted)}} dd{{margin:0;font-weight:600}}
table{{width:100%;border-collapse:collapse;background:var(--panel);
 border:1px solid var(--line);border-radius:8px;overflow:hidden}}
th,td{{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);
 vertical-align:top}}
th{{font-size:10px;text-transform:uppercase;letter-spacing:.07em;
 color:var(--muted);background:var(--bg)}}
td.id{{font-family:ui-monospace,Consolas,monospace;white-space:nowrap;
 font-size:12.5px}}
td.out{{font-weight:700;white-space:nowrap;font-size:12px;text-align:right}}
tr.ok td.out{{color:var(--ok)}} tr.bad td.out{{color:var(--bad)}}
tr.warn td.out{{color:var(--warn)}} tr.info td.out{{color:var(--info)}}
tr.bad{{background:#fdf5f4}}
.sec{{color:var(--muted);font-size:11.5px;margin-top:2px}}
.det{{font-size:13px;margin-top:4px}}
ul.ev{{margin:6px 0 0;padding-left:18px;font-size:12px;color:var(--muted);
 font-family:ui-monospace,Consolas,monospace}}
.counts span{{display:inline-block;margin-right:20px;font-weight:700}}
td.t{{font-family:ui-monospace,monospace;font-size:11.5px;color:var(--muted);
 white-space:nowrap;width:70px}}
td.a{{width:24px;text-align:center;color:var(--muted)}}
td.n{{font-size:11.5px;color:var(--muted);text-align:right;white-space:nowrap}}
tr.conn td{{background:var(--bg);font-weight:700;font-size:12px}}
tr.msg.in td{{color:var(--ink)}} tr.msg.out td{{color:var(--muted)}}
.note{{background:#fffbe9;border:1px solid #e8d99a;border-radius:8px;
 padding:14px 18px;font-size:13px;margin-bottom:18px}}
footer{{color:var(--muted);font-size:12px;margin-top:28px;text-align:center}}
@media print{{body{{background:#fff;padding:0}} .card,table{{break-inside:avoid}}}}
</style></head><body><div class="wrap">

<h1>CUPPS Application Self-Assessment Record</h1>
<div class="sub">Common Use Passenger Processing Systems Technical
Specification {esc(CUPPS_TS_VERSION)}</div>

<div class="note"><b>What this is.</b> {esc(DISCLAIMER)}</div>

<div class="verdict {'ok' if verdict_ok else 'bad'}">
{'No required check failed.' if verdict_ok else
 f'{len(result.blocking_failures)} required check(s) failed &mdash; not ready for compliance testing.'}
</div>

<div class="card"><dl>
<dt>Application</dt><dd>{esc(result.application_name or '(not stated)')}
 {esc(result.application_version)}</dd>
<dt>Run</dt><dd>{esc(result.started)} &rarr; {esc(result.finished)}
 ({result.duration:.0f}s)</dd>
<dt>Scenario</dt><dd>{esc(', '.join(result.steps_run) or 'observation only')}</dd>
<dt>Conversation</dt><dd>{len(result.recorder.ordered())} connection(s),
 {result.recorder.message_count} message(s)</dd>
<dt>Fingerprint</dt><dd>{esc(fingerprint(result))}</dd>
</dl></div>

<div class="card counts">
<span style="color:var(--ok)">{counts['pass']} passed</span>
<span style="color:var(--bad)">{counts['fail']} failed</span>
<span style="color:var(--warn)">{counts['not-exercised']} not exercised</span>
</div>

<h2>Checks</h2>
<table><thead><tr><th>Check</th><th>Requirement</th><th>Outcome</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>

<h2>Scenario log</h2>
<table><tbody>{log_rows or "<tr><td>No scenario steps were driven.</td></tr>"}</tbody></table>

<h2>Recorded conversation</h2>
<table><tbody>{''.join(conversation_rows)}</tbody></table>

<footer>Generated by the CUPPS conformance harness against specification
{esc(CUPPS_TS_VERSION)}.</footer>
</div></body></html>"""
