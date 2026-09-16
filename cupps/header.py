"""CUPPS message-stream framing (TS 01.04.0004 sections 26.8 - 26.9).

Every XML message on a CUPPS socket is prefixed by a fixed-width ASCII header
of the form ``vvllllllll[xxx]``:

* ``vv``       two-byte header version, pattern ``[A-Z0-9]``
* ``llllllll`` eight-byte body length; for version 01 the leading two bytes
  carry a status (``00`` normal, ``FF`` PltMaxMsgSize exceeded) and the
  trailing six are the body length in UPPERCASE hexadecimal (26.8)
* ``[xxx]``    version-defined trailer; version 01 defines none

Only header version 01 is Supported (Table 26.3).  The reader must consume the
header byte-by-byte and stop on the first character that is invalid for a
header, which is what :func:`read_header` does.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import params

#: Total width of a version 01 header, in bytes. Corresponds to PltMsgHdrSize
#: (26.11.31) for this header version.
HEADER_01_SIZE = 10

#: Header version this implementation emits.
HEADER_VERSION = "01"

#: Lowest / highest header versions understood, reported in
#: <invalidVersionNumberInHeader> (Listing 26.1).
MIN_HEADER_VERSION = "01"
MAX_HEADER_VERSION = "01"

#: Status field values occupying header bytes 2-3.
STATUS_OK = "00"
STATUS_TOO_LARGE = "FF"

#: The header alphabet from section 26.8: "[A-Z0-9]".
_HEADER_ALPHABET = frozenset("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")


class HeaderError(Exception):
    """Base class for framing faults that must close the socket."""

    #: sessionErrorEvent eventType to fire, per section 26.6.
    event_type = "headerVersion"


class InvalidHeaderCharacter(HeaderError):
    """A byte outside ``[A-Z0-9]`` appeared in the header (26.9)."""

    event_type = "headerVersion"


class UnsupportedHeaderVersion(HeaderError):
    """The peer used a header version we do not implement (26.8)."""

    event_type = "headerVersion"


class HeaderLengthError(HeaderError):
    """Body length outside the range supported for this header version."""

    event_type = "headerLength"


class HeaderTimeout(HeaderError):
    """Timed out before a complete header arrived (26.6)."""

    event_type = "headerTimeout"


@dataclass(frozen=True)
class MessageHeader:
    """A decoded message header."""

    version: str
    status: str
    body_length: int

    @property
    def is_error(self) -> bool:
        """True when the sender flagged PltMaxMsgSize overflow (Listing 26.1)."""
        return self.status == STATUS_TOO_LARGE


def encode_header(body_length: int, status: str = STATUS_OK) -> bytes:
    """Build a header version 01 prefix for a body of ``body_length`` bytes.

    ``body_length`` counts 8-bit bytes of the UTF-8 encoded body, including any
    continuation bytes of multi-byte characters (26.8.1).
    """
    if body_length < 0 or body_length > params.PLT_MAX_MSG_SIZE:
        raise HeaderLengthError(
            f"body length {body_length} exceeds PltMaxMsgSize "
            f"({params.PLT_MAX_MSG_SIZE})"
        )
    if status not in (STATUS_OK, STATUS_TOO_LARGE):
        raise HeaderError(f"invalid header status field {status!r}")
    # Section 26.8 requires uppercase hexadecimal: "0123abcd is not permitted".
    return f"{HEADER_VERSION}{status}{body_length:06X}".encode("ascii")


def decode_header(raw: bytes) -> MessageHeader:
    """Decode a complete 10-byte version 01 header.

    Raises the specific :class:`HeaderError` subclass matching the
    sessionErrorEvent the caller must fire before closing the socket.
    """
    if len(raw) != HEADER_01_SIZE:
        raise HeaderLengthError(
            f"header must be {HEADER_01_SIZE} bytes, got {len(raw)}"
        )
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise InvalidHeaderCharacter("header contains non-ASCII bytes") from exc

    for char in text:
        if char not in _HEADER_ALPHABET:
            raise InvalidHeaderCharacter(
                f"character {char!r} is not valid in a message header"
            )

    version = text[0:2]
    if version != HEADER_VERSION:
        raise UnsupportedHeaderVersion(
            f"header version {version!r} is not supported"
        )

    status = text[2:4]
    length_field = text[4:10]
    try:
        body_length = int(length_field, 16)
    except ValueError as exc:
        # Unreachable while the alphabet check above passes, but a header
        # version that widens the alphabet would land here.
        raise HeaderLengthError(
            f"length field {length_field!r} is not hexadecimal"
        ) from exc

    if body_length > params.PLT_MAX_MSG_SIZE:
        raise HeaderLengthError(
            f"body length {body_length} exceeds PltMaxMsgSize"
        )
    return MessageHeader(version=version, status=status, body_length=body_length)


def is_header_character(byte: int) -> bool:
    """True when ``byte`` may legally appear in a header (26.9)."""
    return chr(byte) in _HEADER_ALPHABET if 0 <= byte < 128 else False


def invalid_version_frame() -> bytes:
    """Build the complete reply for an unsupported header version.

    Listing 26.1 requires the recipient to answer using header version 01 and
    then terminate the session.
    """
    body = (
        f'<invalidVersionNumberInHeader minVersion="{MIN_HEADER_VERSION}" '
        f'maxVersion="{MAX_HEADER_VERSION}"/>'
    ).encode("utf-8")
    return encode_header(len(body), status=STATUS_TOO_LARGE) + body


def frame(body: bytes) -> bytes:
    """Prefix an encoded message body with its header."""
    return encode_header(len(body)) + body
