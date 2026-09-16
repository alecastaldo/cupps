"""The conformance rule catalogue for CUPPS applications.

Each check states one obligation the Technical Specification places on an
*application*, cites the clause it comes from, and evaluates it against a
recorded conversation (:mod:`recorder`).

Checks carry a severity:

``required``
    The specification says must / shall / CRITICAL.  A failure here will fail
    formal Application Compliance Testing.
``recommended``
    The specification says should, or names the behaviour as indicating a bug.
    A failure will not necessarily block certification but reviewers ask about
    it.
``informational``
    Observations that help a reviewer, with no pass or fail.

``atc_reference`` is left blank deliberately: the official CUPPS Application
Test Cases (``CUPPS-ATC-01.04.*`` on the IATA MS Teams site) carry their own
case numbers, and a site that maps them in here gets a report that lines up
with what a Compliance Testing Entity works from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from cupps import params
from cupps.results import MODES_BY_DEVICE, InterfaceMode

from .recorder import Connection, RecordedMessage, Recorder

REQUIRED = "required"
RECOMMENDED = "recommended"
INFORMATIONAL = "informational"

PASS = "pass"
FAIL = "fail"
NOT_EXERCISED = "not-exercised"
INFO = "info"


@dataclass
class Finding:
    """The outcome of one check."""

    check_id: str
    title: str
    section: str
    severity: str
    outcome: str
    detail: str = ""
    #: Message-level evidence, quoted into the report.
    evidence: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return self.outcome == FAIL and self.severity == REQUIRED

    def to_dict(self) -> dict:
        return {
            "checkId": self.check_id,
            "title": self.title,
            "section": self.section,
            "severity": self.severity,
            "outcome": self.outcome,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass
class Check:
    """One rule in the catalogue."""

    check_id: str
    title: str
    section: str
    severity: str
    run: Callable[[Recorder], Finding]
    atc_reference: str = ""


_CATALOGUE: list[Check] = []


def check(check_id: str, title: str, section: str, severity: str = REQUIRED):
    """Register a check in the catalogue."""

    def decorator(function: Callable[[Recorder], tuple]) -> Callable:
        def run(recorder: Recorder) -> Finding:
            outcome, detail, evidence = _normalise(function(recorder))
            return Finding(
                check_id=check_id,
                title=title,
                section=section,
                severity=severity,
                outcome=outcome,
                detail=detail,
                evidence=evidence,
            )

        _CATALOGUE.append(
            Check(
                check_id=check_id,
                title=title,
                section=section,
                severity=severity,
                run=run,
            )
        )
        return function

    return decorator


def _normalise(result) -> tuple[str, str, list[str]]:
    if isinstance(result, tuple):
        outcome = result[0]
        detail = result[1] if len(result) > 1 else ""
        evidence = list(result[2]) if len(result) > 2 else []
        return outcome, detail, evidence
    return result, "", []


def catalogue() -> list[Check]:
    """Every registered check, in catalogue order."""
    return list(_CATALOGUE)


def run_all(recorder: Recorder) -> list[Finding]:
    """Evaluate the whole catalogue against a recording."""
    return [check.run(recorder) for check in _CATALOGUE]


def _quote(message: RecordedMessage) -> str:
    return (
        f"conn {message.connection_id} "
        f"{'app->plt' if message.is_from_application else 'plt->app'} "
        f"{message.message_name} id={message.message_id} at +{message.at:.2f}s"
    )


# ---------------------------------------------------------------------------
# Handshake and session establishment
# ---------------------------------------------------------------------------


@check("APP-0010", "Every connection opens with the interface level handshake",
       "26.6, 28.2")
def first_message_is_the_handshake(recorder: Recorder):
    offenders = []
    checked = 0
    for connection in recorder.ordered():
        first = connection.first_inbound()
        if first is None:
            continue
        checked += 1
        if first.message_name != "interfaceLevelsAvailableRequest":
            offenders.append(_quote(first))
    if not checked:
        return NOT_EXERCISED, "no application traffic was recorded", []
    if offenders:
        return (
            FAIL,
            "the first message on a connection must be "
            "<interfaceLevelsAvailableRequest>",
            offenders,
        )
    return PASS, f"{checked} connection(s) opened correctly", []


@check("APP-0011", "The chosen interface level was one the platform offered",
       "26.4, 28.3")
def chosen_level_was_offered(recorder: Recorder):
    offenders = []
    chosen = set()
    for connection in recorder.ordered():
        offers = connection.outbound("interfaceLevelsAvailableResponse")
        requests = connection.inbound("interfaceLevelRequest")
        if not offers or not requests:
            continue
        available = {
            element.get("level")
            for element in offers[0].body.findall("interfaceLevel")
        }
        level = requests[0].attr("level")
        chosen.add(level)
        if level not in available:
            offenders.append(
                f"{_quote(requests[0])}: chose {level!r}, offered {sorted(available)}"
            )
    if not chosen:
        return NOT_EXERCISED, "no interface level was negotiated", []
    if offenders:
        return FAIL, "an unoffered interface level was selected", offenders
    return PASS, f"negotiated {', '.join(sorted(str(c) for c in chosen))}", []


@check("APP-0012", "Messages use the namespace of the chosen interface level",
       "26.4, 26.12")
def namespace_matches_level(recorder: Recorder):
    from cupps.xmlmsg import HANDSHAKE_MESSAGES, NAMESPACE_BY_LEVEL

    offenders = []
    for connection in recorder.ordered():
        if not connection.interface_level:
            continue
        expected = NAMESPACE_BY_LEVEL.get(connection.interface_level)
        for message in connection.inbound():
            if message.message_name in HANDSHAKE_MESSAGES:
                continue
            if expected and message.namespace != expected:
                offenders.append(
                    f"{_quote(message)}: namespace {message.namespace!r}, "
                    f"expected {expected!r}"
                )
    if offenders:
        return FAIL, "messages did not carry the negotiated namespace", offenders[:10]
    return PASS, "", []


@check("APP-0020", "<authenticateRequest> is sent once, on the platform "
       "connection only", "26.6 CRITICAL")
def authenticate_once_on_platform_only(recorder: Recorder):
    total = 0
    offenders = []
    for connection in recorder.ordered():
        requests = connection.inbound("authenticateRequest")
        total += len(requests)
        if len(requests) > 1:
            offenders.append(
                f"conn {connection.connection_id}: {len(requests)} "
                f"<authenticateRequest> on one connection"
            )
        if requests and connection.is_device_connection:
            offenders.append(
                f"conn {connection.connection_id}: <authenticateRequest> on a "
                f"device connection; device sessions must use "
                f"<deviceAcquireRequest>"
            )
    if total == 0:
        return NOT_EXERCISED, "the application never authenticated", []
    if offenders:
        return FAIL, "authentication rule violated", offenders
    return PASS, f"{total} authentication(s), one per platform connection", []


@check("APP-0023", "<deviceAcquireRequest> is sent at most once per device "
       "session", "26.6 NOTE")
def acquire_once_per_session(recorder: Recorder):
    offenders = []
    sessions = 0
    for connection in recorder.device_connections():
        requests = connection.inbound("deviceAcquireRequest")
        if requests:
            sessions += 1
        if len(requests) > 1:
            offenders.append(
                f"conn {connection.connection_id}: {len(requests)} acquires; "
                f"a second one is an illogical message"
            )
    if not sessions:
        return NOT_EXERCISED, "no device was acquired", []
    if offenders:
        return FAIL, "a device session acquired more than once", offenders
    return PASS, f"{sessions} device session(s)", []


@check("APP-0030", "<byeRequest> is sent before closing the platform connection",
       "29.1 REQUIREMENT")
def bye_before_close(recorder: Recorder):
    offenders = []
    closed = 0
    for connection in recorder.platform_connections():
        if not connection.inbound("authenticateRequest"):
            continue
        if not connection.closed:
            continue
        if connection.connection_id in recorder.harness_closed:
            # The harness killed this one to test recovery; the application
            # was never given the chance to say goodbye.
            continue
        closed += 1
        if not connection.inbound("byeRequest"):
            offenders.append(
                f"conn {connection.connection_id}: closed without <byeRequest>"
            )
    if not closed:
        return NOT_EXERCISED, "no platform connection was closed during the run", []
    if offenders:
        return (
            FAIL,
            "releasing a session without informing the platform may indicate a "
            "bug and must be avoided",
            offenders,
        )
    return PASS, f"{closed} platform connection(s) closed cleanly", []


@check("APP-0031", "No messages are sent after <byeRequest>", "29.1 CRITICAL")
def nothing_after_bye(recorder: Recorder):
    offenders = []
    for connection in recorder.ordered():
        byes = connection.inbound("byeRequest")
        if not byes:
            continue
        after = [
            message
            for message in connection.inbound()
            if message.at > byes[0].at and message.message_name != "byeRequest"
        ]
        offenders.extend(_quote(message) for message in after)
    if offenders:
        return FAIL, "messages were sent after the application said goodbye", offenders
    return PASS, "", []


@check("APP-0032", "<deviceReleaseRequest> is sent before closing a device "
       "connection", "26.6, 30.6.2")
def release_before_close(recorder: Recorder):
    offenders = []
    closed = 0
    for connection in recorder.device_connections():
        if not connection.inbound("deviceAcquireRequest") or not connection.closed:
            continue
        closed += 1
        if not connection.inbound("deviceReleaseRequest"):
            offenders.append(
                f"conn {connection.connection_id} ({connection.device_name}): "
                f"closed without <deviceReleaseRequest>"
            )
    if not closed:
        return NOT_EXERCISED, "no device connection was closed during the run", []
    if offenders:
        return FAIL, "device sessions were closed without being released", offenders
    return PASS, f"{closed} device session(s) released cleanly", []


# ---------------------------------------------------------------------------
# Framing and message identity
# ---------------------------------------------------------------------------


@check("APP-0042", "No message exceeds PltMaxMsgSize", "26.11.30")
def message_size_within_limit(recorder: Recorder):
    offenders = [
        f"{_quote(message)}: {message.raw_length} bytes"
        for message in recorder.all_messages()
        if message.is_from_application
        and message.raw_length > params.PLT_MAX_MSG_SIZE
    ]
    if offenders:
        return FAIL, f"PltMaxMsgSize is {params.PLT_MAX_MSG_SIZE} bytes", offenders
    largest = max(
        (m.raw_length for m in recorder.all_messages() if m.is_from_application),
        default=0,
    )
    return PASS, f"largest application message was {largest} bytes", []


@check("APP-0050", "Application messageIDs stay inside the application range",
       "27.1.5, 26.11.21")
def message_ids_in_application_range(recorder: Recorder):
    """Only *originated* messages are bound by the range.

    Section 27.1.5 constrains "application-originated messages". A reply is
    not one: section 26.12 requires every response to echo the messageID of
    its request, so an answer to a platform-originated request such as
    <applicationStopCommandRequest> correctly carries a platform-range ID.
    """
    low, high = params.MIN_MESSAGE_ID, params.MIN_PLATFORM_MSG_ID - 1
    offenders = []
    for connection in recorder.ordered():
        # messageIDs the platform originated on this connection, which the
        # application is entitled to echo back.
        platform_originated = {
            message.message_id
            for message in connection.outbound()
            if not message.message_name.endswith("Response")
        }
        for message in connection.inbound():
            if low <= message.message_id <= high:
                continue
            if (
                message.message_name.endswith("Response")
                and message.message_id in platform_originated
            ):
                continue
            offenders.append(
                f"{_quote(message)}: messageID {message.message_id} outside "
                f"{low}..{high}"
            )
    if offenders:
        return (
            FAIL,
            "application-originated messages must use MinMessageID through "
            "MinPlatformMsgID-1",
            offenders[:10],
        )
    return PASS, "responses correctly echo platform-originated messageIDs", []


@check("APP-0052", "Outstanding messages stay within PltStreamOutMsgs",
       "26.11.35, 26.11.36")
def outstanding_messages_within_limit(recorder: Recorder):
    offenders = []
    for connection in recorder.ordered():
        limit = (
            params.PLT_STREAM_OUT_MSGS_ZL
            if connection.device_type == "ZL" or connection.device_name.upper().find("ZL") >= 0
            else params.PLT_STREAM_OUT_MSGS
        )
        pending: set[int] = set()
        peak = 0
        for message in connection.messages:
            if message.is_from_application:
                if message.message_name.endswith("Request"):
                    pending.add(message.message_id)
                    peak = max(peak, len(pending))
            else:
                pending.discard(message.message_id)
        if peak > limit:
            offenders.append(
                f"conn {connection.connection_id} ({connection.label}): peaked "
                f"at {peak} outstanding request(s), limit {limit}"
            )
    if offenders:
        return FAIL, "too many overlapped messages on a socket", offenders
    return PASS, "", []


# ---------------------------------------------------------------------------
# Device usage
# ---------------------------------------------------------------------------


@check("APP-0060", "<interfaceModeRequest> follows device acquisition", "30.1")
def interface_mode_after_acquire(recorder: Recorder):
    offenders = []
    checked = 0
    for connection in recorder.device_connections():
        acquires = connection.inbound("deviceAcquireRequest")
        if not acquires:
            continue
        # A refused acquire ends the session; no mode is expected.
        responses = connection.outbound("deviceAcquireResponse")
        if responses and not (responses[0].attr("result") or "").startswith("OK"):
            continue
        checked += 1
        modes = connection.inbound("interfaceModeRequest")
        if not modes:
            offenders.append(
                f"conn {connection.connection_id} ({connection.device_name}): "
                f"no <interfaceModeRequest> after acquiring"
            )
        elif modes[0].at < acquires[0].at:
            offenders.append(
                f"conn {connection.connection_id}: interface mode set before "
                f"the device was acquired"
            )
    if not checked:
        return NOT_EXERCISED, "no device was successfully acquired", []
    if offenders:
        return (
            FAIL,
            "applications must issue <interfaceModeRequest> immediately after "
            "acquiring a device",
            offenders,
        )
    return PASS, f"{checked} device session(s) set their interface mode", []


@check("APP-0061", "The requested interface mode is one the device supports",
       "Table 30.1")
def mode_supported_by_device_type(recorder: Recorder):
    offenders = []
    for connection in recorder.device_connections():
        if not connection.interface_mode or not connection.device_name:
            continue
        device_type = _device_type_of(connection)
        if not device_type:
            continue
        allowed = MODES_BY_DEVICE.get(device_type, set())
        if not allowed:
            continue
        try:
            requested = InterfaceMode(connection.interface_mode)
        except ValueError:
            offenders.append(
                f"{connection.device_name}: unknown mode "
                f"{connection.interface_mode!r}"
            )
            continue
        if requested not in allowed:
            offenders.append(
                f"{connection.device_name} ({device_type}): requested "
                f"{requested.value}, Table 30.1 allows "
                f"{sorted(m.value for m in allowed)}"
            )
    if offenders:
        return FAIL, "an unsupported interface mode was requested", offenders
    return PASS, "", []


@check("APP-0062", "An AEA Mode session sends EP as its first AEA command",
       "30.1 CRITICAL")
def aea_session_sends_ep_first(recorder: Recorder):
    offenders = []
    checked = 0
    for connection in recorder.device_connections():
        if connection.interface_mode != InterfaceMode.AEA.value:
            continue
        aea = connection.inbound("aeaRequest")
        if not aea:
            offenders.append(
                f"{connection.device_name}: AEA Mode session sent no AEA command; "
                f"EP is required immediately after the mode is set"
            )
            continue
        checked += 1
        first = _aea_text(aea[0])
        if not first.startswith("EP"):
            offenders.append(
                f"{connection.device_name}: first AEA command was {first[:24]!r}, "
                f"expected EP"
            )
    if not checked and not offenders:
        return NOT_EXERCISED, "no AEA Mode session was opened", []
    if offenders:
        return FAIL, "AEA sessions must open with the EP command", offenders
    return PASS, f"{checked} AEA session(s) opened with EP", []


@check("APP-0063", "No <aeaRequest> on a Standard Mode session", "30.15 NOTE")
def no_aea_in_standard_mode(recorder: Recorder):
    offenders = [
        f"{connection.device_name}: {_quote(message)}"
        for connection in recorder.device_connections()
        if connection.interface_mode == InterfaceMode.STANDARD.value
        for message in connection.inbound("aeaRequest")
    ]
    if offenders:
        return (
            FAIL,
            "sending <aeaRequest> in Standard Mode results in an Illogical "
            "Message error",
            offenders,
        )
    return PASS, "", []


@check("APP-0064", "Special Mode devices are never locked", "30.20 NOTE")
def no_locking_special_mode(recorder: Recorder):
    offenders = []
    for connection in recorder.device_connections():
        if connection.interface_mode != InterfaceMode.SPECIAL.value:
            continue
        for message in connection.inbound("deviceLockRequest", "deviceUnlockRequest"):
            offenders.append(f"{connection.device_name}: {_quote(message)}")
    if offenders:
        return (
            FAIL,
            "attempting to lock or unlock a Special Mode device results in an "
            "<illogicalMessageErrorEvent>",
            offenders,
        )
    return PASS, "", []


@check("APP-0065", "Device status is not polled faster than DevPollMaxFreq",
       "26.11.9", RECOMMENDED)
def polling_within_limit(recorder: Recorder):
    offenders = []
    polls = 0
    for connection in recorder.device_connections():
        requests = connection.inbound("deviceStatusRequest")
        polls += len(requests)
        for previous, current in zip(requests, requests[1:]):
            gap = current.at - previous.at
            if gap < params.DEV_POLL_MAX_FREQ * (1 - params.TIME_TOLERANCE):
                offenders.append(
                    f"{connection.device_name}: {gap:.2f}s between polls, "
                    f"DevPollMaxFreq is {params.DEV_POLL_MAX_FREQ}s"
                )
    if not polls:
        return PASS, "no status polling; the application relies on <notify>", []
    if offenders:
        return (
            FAIL,
            "excessive polling must be answered with pollingTooFast and makes "
            "the application non-compliant",
            offenders[:10],
        )
    return PASS, f"{polls} status poll(s), all within DevPollMaxFreq", []


@check("APP-0066", "A held lock is not renewed faster than half DevLkdTime",
       "30.7 REQUIREMENT", RECOMMENDED)
def lock_renewal_not_excessive(recorder: Recorder):
    threshold = params.DEV_LKD_TIME / 2
    offenders = []
    for connection in recorder.device_connections():
        locks = connection.inbound("deviceLockRequest")
        for previous, current in zip(locks, locks[1:]):
            gap = current.at - previous.at
            if gap < threshold * (1 - params.TIME_TOLERANCE):
                offenders.append(
                    f"{connection.device_name}: {gap:.2f}s between lock "
                    f"requests, half DevLkdTime is {threshold}s"
                )
    if offenders:
        return (
            FAIL,
            "issuing <deviceLockRequest> more often than half DevLkdTime is "
            "considered a bug",
            offenders[:10],
        )
    return PASS, "", []


@check("APP-0067", "Lock methods are not switched while a lock is held",
       "30.7 REQUIREMENT")
def no_lock_method_switching(recorder: Recorder):
    offenders = []
    for connection in recorder.device_connections():
        held: Optional[str] = None
        for message in connection.inbound(
            "deviceLockRequest", "deviceUnlockRequest"
        ):
            if message.message_name == "deviceUnlockRequest":
                held = None
                continue
            method = message.attr("lockMethod") or "byConnection"
            if held is not None and method != held:
                offenders.append(
                    f"{connection.device_name}: switched from {held} to "
                    f"{method} while holding the lock"
                )
            held = method
    if offenders:
        return (
            FAIL,
            "switching lock methods causes an <illogicalMessageErrorEvent>",
            offenders,
        )
    return PASS, "", []


@check("APP-0080", "Applications do not write to platform-scope logs",
       "30.20.1 CRITICAL")
def no_platform_scope_logging(recorder: Recorder):
    offenders = []
    for connection in recorder.device_connections():
        for message in connection.inbound("logOpenRequest"):
            if (message.attr("scope") or "") == "platform":
                offenders.append(f"{connection.device_name}: {_quote(message)}")
    if offenders:
        return (
            FAIL,
            "applications are NOT allowed to write to platform scope logs",
            offenders,
        )
    return PASS, "", []


# ---------------------------------------------------------------------------
# Recovery -- called out explicitly by section 17.1.2
# ---------------------------------------------------------------------------


@check("APP-0070", "The application recovers device sessions after loss of the "
       "platform session", "17.1.2, 26.7")
def recovers_after_platform_loss(recorder: Recorder):
    if not any("platform session dropped" in note for _, note in recorder.notes):
        return (
            NOT_EXERCISED,
            "the platform-loss scenario was not run; section 17.1.2 requires "
            "compliance testing to include full device session recovery",
            [],
        )

    drop_at = min(
        at for at, note in recorder.notes if "platform session dropped" in note
    )
    reauth = [
        message
        for message in recorder.all_messages()
        if message.is_from_application
        and message.message_name == "authenticateRequest"
        and message.at > drop_at
    ]
    if not reauth:
        return (
            FAIL,
            "the application did not re-authenticate after the platform "
            "session was lost",
            [f"session dropped at +{drop_at:.2f}s; no later <authenticateRequest>"],
        )

    reacquired = [
        connection
        for connection in recorder.device_connections()
        if connection.opened_at > reauth[0].at
        and connection.inbound("deviceAcquireRequest")
    ]
    if not reacquired:
        return (
            FAIL,
            "the application re-authenticated but did not rebuild its device "
            "sessions",
            [f"re-authenticated at +{reauth[0].at:.2f}s; no device re-acquired"],
        )
    return (
        PASS,
        f"re-authenticated {reauth[0].at - drop_at:.1f}s after the drop and "
        f"rebuilt {len(reacquired)} device session(s)",
        [],
    )


@check("APP-0072", "An invalidated device token is never reused", "26.7 CRITICAL")
def no_reuse_of_invalidated_token(recorder: Recorder):
    if len(recorder.issued_tokens) < 2:
        return NOT_EXERCISED, "only one device token was issued", []
    offenders = []
    for connection in recorder.device_connections():
        for message in connection.inbound("deviceAcquireRequest"):
            token = message.attr("deviceToken") or ""
            invalidated_at = recorder.invalidated_tokens.get(token)
            # A token is only stale *after* it was invalidated; its use before
            # that is exactly what it was issued for.
            if invalidated_at is not None and message.at > invalidated_at:
                offenders.append(
                    f"{connection.device_name}: acquired at +{message.at:.2f}s "
                    f"with a token invalidated at +{invalidated_at:.2f}s"
                )
    if offenders:
        return (
            FAIL,
            "device tokens are invalidated when the platform connection ends; "
            "all further operations with them are invalid",
            offenders,
        )
    return PASS, "each device session used the current token", []


# ---------------------------------------------------------------------------
# Informational observations
# ---------------------------------------------------------------------------


@check("APP-0090", "Devices exercised during the run", "11.2.5", INFORMATIONAL)
def devices_exercised(recorder: Recorder):
    seen: dict[str, str] = {}
    for connection in recorder.device_connections():
        if connection.device_name:
            seen[connection.device_name] = connection.interface_mode or "-"
    if not seen:
        return INFO, "no devices were exercised", []
    return (
        INFO,
        f"{len(seen)} device(s) exercised",
        [f"{name} ({mode})" for name, mode in sorted(seen.items())],
    )


@check("APP-0091", "Message types exercised", "25", INFORMATIONAL)
def messages_exercised(recorder: Recorder):
    counts: dict[str, int] = {}
    for message in recorder.all_messages():
        if message.is_from_application:
            counts[message.message_name] = counts.get(message.message_name, 0) + 1
    if not counts:
        return INFO, "no application messages were recorded", []
    return (
        INFO,
        f"{len(counts)} distinct application message type(s), "
        f"{sum(counts.values())} message(s) total",
        [f"{name} x{count}" for name, count in sorted(counts.items())],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _device_type_of(connection: Connection) -> str:
    """Infer the two-letter device type from the acquired device name."""
    if connection.device_type:
        return connection.device_type.upper()
    name = connection.device_name.upper()
    for code in MODES_BY_DEVICE:
        # Device names end with the type and an index (section 4.6.1).
        index = name.rfind(code)
        if index >= 0 and name[index + len(code):].isdigit():
            return code
    return ""


def _aea_text(message: RecordedMessage) -> str:
    """Reassemble the text of an <aeaRequest> for inspection."""
    import base64

    chunks = []
    for aea_message in message.body.findall("aeaMessage"):
        for child in aea_message.children:
            if child.name == "aeaText":
                chunks.append(child.text or "")
            elif child.name == "aeaBinary" and child.text:
                try:
                    chunks.append(
                        base64.b64decode("".join(child.text.split())).decode(
                            "latin-1", "replace"
                        )
                    )
                except Exception:
                    chunks.append("<binary>")
    return "".join(chunks)
