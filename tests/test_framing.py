"""Message stream framing, against the examples in sections 26.8 and 26.9."""

from __future__ import annotations

import pytest

from cupps import header, params


def test_sample_message_matches_listing_26_3():
    """Listing 26.3 shows a 19-byte body framed as ``0100000013``."""
    assert header.frame(b"<CUPPS>body</CUPPS>") == b"0100000013<CUPPS>body</CUPPS>"


def test_invalid_version_reply_matches_listing_26_1():
    """Listing 26.1 shows the reply framed as ``01FF00003F``."""
    frame = header.invalid_version_frame()
    assert frame.startswith(b"01FF00003F")
    assert len(frame) - header.HEADER_01_SIZE == 0x3F


def test_length_field_is_uppercase_hexadecimal():
    """Section 26.8: "0123abcd is not permitted, as it must be uppercase"."""
    encoded = header.encode_header(0xABCDEF)
    assert encoded == b"0100ABCDEF"
    assert encoded.decode("ascii").isupper() or encoded.decode("ascii").isdigit()


def test_round_trip():
    for length in (0, 1, 19, 0xFFFF, params.PLT_MAX_MSG_SIZE):
        assert header.decode_header(header.encode_header(length)).body_length == length


def test_body_over_plt_max_msg_size_is_refused():
    with pytest.raises(header.HeaderLengthError):
        header.encode_header(params.PLT_MAX_MSG_SIZE + 1)


def test_invalid_header_character_is_rejected():
    """Section 26.9: reading stops on the first character invalid for a header."""
    with pytest.raises(header.InvalidHeaderCharacter):
        header.decode_header(b"01000000a3")  # lowercase is outside [A-Z0-9]
    with pytest.raises(header.InvalidHeaderCharacter):
        header.decode_header(b"01000000-3")


def test_unsupported_header_version_is_rejected():
    """Table 26.3 lists only version 01 as Supported."""
    with pytest.raises(header.UnsupportedHeaderVersion):
        header.decode_header(b"0200000013")


def test_header_alphabet_matches_the_specified_pattern():
    """The pattern in section 26.8 is exactly ``[A-Z0-9]``.

    Deliberately spelled out as ASCII ranges rather than with ``str.isdigit``,
    which is Unicode-aware and accepts characters such as U+00B2 that the
    pattern excludes.
    """
    for code in range(256):
        char = chr(code)
        expected = ("0" <= char <= "9") or ("A" <= char <= "Z")
        assert header.is_header_character(code) is expected, code
