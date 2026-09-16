"""Message envelope and messageID rules (sections 26.12, 27.1.5)."""

from __future__ import annotations

import pytest

from cupps import params, xmlmsg
from cupps.msgid import MessageIdGenerator, MessageIdTracker, PendingRequests


def test_interface_level_request_matches_listing_28_3():
    message = xmlmsg.build("interfaceLevelRequest", 2, attrs={"level": "01.04"})
    text = message.encode().decode("utf-8")
    assert '<interfaceLevelRequest level="01.04"/>' in text
    assert 'messageName="interfaceLevelRequest"' in text
    assert 'messageID="2"' in text


def test_handshake_messages_carry_no_default_namespace():
    """The handshake schema is separate (section 26.6 note 4, Listing 28.1)."""
    handshake = xmlmsg.build("interfaceLevelsAvailableRequest", 1)
    assert "xmlns=" not in handshake.encode().decode("utf-8")

    main = xmlmsg.build("byeRequest", 1)
    assert 'xmlns="http://www.cupps.aero/cupps/01.04"' in main.encode().decode("utf-8")


def test_element_order_is_preserved():
    """Section 26.12 CRITICAL: elements are processed in the order sent."""
    body = xmlmsg.Element("printRequest")
    for index in (3, 1, 2):
        body.add(xmlmsg.Element("printDocument", {"documentID": str(index)}))
    text = xmlmsg.build("printRequest", 1, body=body).encode().decode("utf-8")
    # Match the full attribute: a bare '"1"' would also hit messageID="1".
    positions = [text.index(f'documentID="{n}"') for n in (3, 1, 2)]
    assert positions == sorted(positions)


def test_round_trip_through_the_parser():
    body = xmlmsg.Element("authenticateRequest", {"airline": "BA"})
    body.add(xmlmsg.Element("applicationList")).add(
        xmlmsg.Element("application", {"applicationName": "X", "applicationVersion": "1"})
    )
    parsed = xmlmsg.parse(xmlmsg.build("authenticateRequest", 7, body=body).encode())
    assert parsed.message_id == 7
    assert parsed.body.get("airline") == "BA"
    assert parsed.body.find("applicationList").findall("application")[0].get(
        "applicationName"
    ) == "X"


def test_attributes_are_escaped():
    message = xmlmsg.build("byeRequest", 1, attrs={"note": 'a & b <c> "d"'})
    text = message.encode().decode("utf-8")
    assert "&amp;" in text and "&lt;" in text and "&quot;" in text
    assert xmlmsg.parse(message.encode()).body.get("note") == 'a & b <c> "d"'


@pytest.mark.parametrize(
    "payload",
    [
        b"not xml at all",
        b"<notcupps/>",
        b'<cupps messageName="x"><x/></cupps>',          # no messageID
        b'<cupps messageID="1"><x/></cupps>',            # no messageName
        b'<cupps messageID="1" messageName="a"><b/></cupps>',  # name mismatch
        b'<cupps messageID="x" messageName="a"><a/></cupps>',  # non-numeric ID
    ],
)
def test_malformed_messages_raise_parse_error(payload):
    with pytest.raises(xmlmsg.MessageParseError):
        xmlmsg.parse(payload)


def test_doctype_is_refused():
    """Entity expansion is the classic XML denial-of-service vector."""
    with pytest.raises(xmlmsg.MessageParseError, match="DOCTYPE"):
        xmlmsg.parse(b'<!DOCTYPE x [<!ENTITY a "b">]><cupps messageID="1" '
                     b'messageName="a"><a/></cupps>')


def test_ok_dash_result_counts_as_success():
    """``OK-deviceAlreadyLocked`` is a success result (section 30.7)."""
    body = xmlmsg.Element("deviceLockResponse", {"result": "OK-deviceAlreadyLocked"})
    assert xmlmsg.Message("deviceLockResponse", 1, body).is_ok


def test_application_and_platform_id_ranges_do_not_overlap():
    """Section 27.1.5 splits the range at MinPlatformMsgID."""
    application = MessageIdGenerator(platform_side=False)
    platform = MessageIdGenerator(platform_side=True)
    assert application.range == (params.MIN_MESSAGE_ID, params.MIN_PLATFORM_MSG_ID - 1)
    assert platform.range == (params.MIN_PLATFORM_MSG_ID, params.MAX_MESSAGE_ID)
    assert not application.owns(params.MIN_PLATFORM_MSG_ID)
    assert platform.owns(params.MIN_PLATFORM_MSG_ID)


def test_message_ids_wrap_within_their_own_range():
    generator = MessageIdGenerator(platform_side=False)
    generator._next = params.MIN_PLATFORM_MSG_ID - 1
    assert generator.allocate() == params.MIN_PLATFORM_MSG_ID - 1
    assert generator.allocate() == params.MIN_MESSAGE_ID


def test_recent_id_list_is_bounded_by_msg_id_list_len():
    tracker = MessageIdTracker()
    for value in range(params.MSG_ID_LIST_LEN + 50):
        tracker.record(value)
    assert len(tracker) == params.MSG_ID_LIST_LEN
    assert 0 not in tracker
    assert params.MSG_ID_LIST_LEN + 49 in tracker


def test_outstanding_messages_are_capped():
    """PltStreamOutMsgs bounds overlapped requests (section 26.11.35)."""
    pending = PendingRequests(limit=params.PLT_STREAM_OUT_MSGS)
    for index in range(params.PLT_STREAM_OUT_MSGS):
        pending.add(index, object())
    assert not pending.has_capacity()
    with pytest.raises(OverflowError):
        pending.add(99, object())
    pending.pop(0)
    assert pending.has_capacity()
