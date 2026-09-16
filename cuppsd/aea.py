"""AEA Mode helpers for BP, BT, BG, SD and SN devices.

CUPPS carries AEA traffic rather than defining it: ``<aeaRequest>`` wraps a
command stream that the airline's host produces and the device's firmware
interprets (section 30.12).  The AEA command set itself lives in the AEA
specification, which CUPPS references but does not reproduce, so this module
deliberately does **not** invent command syntax.  What it does cover is the
part CUPPS *does* define -- how a command is split across ``<aeaText>`` and
``<aeaBinary>`` elements, and the mandatory opening ``EP``.

A handling application therefore takes the AEA stream from its host system and
passes it through :func:`segment`, rather than having this library compose
print commands it cannot verify.
"""

from __future__ import annotations

from typing import Iterable, Union

AeaPart = Union[str, bytes]

#: Section 30.1 CRITICAL: the first command on any AEA Mode session, which
#: puts the device on AEA default parameters.
EP_COMMAND = "EP"


def segment(stream: Union[str, bytes]) -> list[AeaPart]:
    """Split an AEA stream into text and binary parts for ``<aeaRequest>``.

    The receiver rebuilds the command by concatenating ``<aeaText>`` content
    and the Base64 decode of ``<aeaBinary>`` content in order (Table 30.4), so
    printable runs travel as text and everything else as binary.  Keeping
    printable runs as text is what makes a captured message readable to
    support staff, which is the whole point of the split.
    """
    if isinstance(stream, str):
        # A str with no control characters needs no splitting at all.
        if all(_is_aea_text_char(ord(ch)) for ch in stream):
            return [stream]
        data = stream.encode("latin-1", "replace")
    else:
        data = stream

    parts: list[AeaPart] = []
    run = bytearray()
    run_is_text = True

    def flush() -> None:
        if not run:
            return
        parts.append(
            run.decode("latin-1") if run_is_text else bytes(run)
        )
        run.clear()

    for byte in data:
        is_text = _is_aea_text_char(byte)
        if run and is_text != run_is_text:
            flush()
        run_is_text = is_text
        run.append(byte)
    flush()
    return parts


def _is_aea_text_char(byte: int) -> bool:
    """True for characters safe to carry in ``<aeaText>``.

    Restricted to printable ASCII: anything outside it either needs XML
    escaping or is genuinely binary, and both belong in ``<aeaBinary>``.
    Chapter 33 fixes this same printable set for the schema patterns.
    """
    return 0x20 <= byte <= 0x7E


def join(parts: Iterable[AeaPart]) -> bytes:
    """Rebuild a stream from its parts, as the receiving side does."""
    out = bytearray()
    for part in parts:
        out += part.encode("latin-1", "replace") if isinstance(part, str) else part
    return bytes(out)
