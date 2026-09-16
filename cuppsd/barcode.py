"""Barcode symbol generation for printed documents.

CUPPS itself never renders barcodes -- it carries either an AEA command
stream, where the printer's own firmware draws the symbol, or a PDF that the
application has already laid out.  This module covers the second case.

Code 39 (CUPPS barcode type ``3``, Table 30.3) is implemented in full: it is
self-checking, needs no error-correction tables, and is the right symbology
for the numeric and alphanumeric payloads a handling application prints most
-- bag tag licence plate numbers, PNR and sequence references.

Resolution 792 boarding passes use a 2D symbology (PDF417 on paper stock,
Aztec or QR on mobile).  Those need large error-correction tables that are not
worth carrying here, so :func:`register_renderer` lets a site plug in an
encoder of its own.  The AEA path used for BP and BT devices does not need one
at all, because the printer generates the symbol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

#: Code 39: each character is nine elements -- five bars and four spaces,
#: alternating, of which exactly three are wide.
CODE39_PATTERNS = {
    "0": "nnnwwnwnn", "1": "wnnwnnnnw", "2": "nnwwnnnnw", "3": "wnwwnnnnn",
    "4": "nnnwwnnnw", "5": "wnnwwnnnn", "6": "nnwwwnnnn", "7": "nnnwnnwnw",
    "8": "wnnwnnwnn", "9": "nnwwnnwnn", "A": "wnnnnwnnw", "B": "nnwnnwnnw",
    "C": "wnwnnwnnn", "D": "nnnnwwnnw", "E": "wnnnwwnnn", "F": "nnwnwwnnn",
    "G": "nnnnnwwnw", "H": "wnnnnwwnn", "I": "nnwnnwwnn", "J": "nnnnwwwnn",
    "K": "wnnnnnnww", "L": "nnwnnnnww", "M": "wnwnnnnwn", "N": "nnnnwnnww",
    "O": "wnnnwnnwn", "P": "nnwnwnnwn", "Q": "nnnnnnwww", "R": "wnnnnnwwn",
    "S": "nnwnnnwwn", "T": "nnnnwnwwn", "U": "wwnnnnnnw", "V": "nwwnnnnnw",
    "W": "wwwnnnnnn", "X": "nwnnwnnnw", "Y": "wwnnwnnnn", "Z": "nwwnwnnnn",
    "-": "nwnnnnwnw", ".": "wwnnnnwnn", " ": "nwwnnnwnn", "$": "nwnwnwnnn",
    "/": "nwnwnnnwn", "+": "nwnnnwnwn", "%": "nnnwnwnwn", "*": "nwnnwnwnn",
}

#: Values used for the modulo-43 check character, in the standard order.
CODE39_CHECK_ORDER = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-. $/+%"

#: Ratio of a wide element to a narrow one. The symbology allows 2.0 to 3.0;
#: 3.0 gives scanners the most margin on thermal stock.
WIDE_RATIO = 3.0


class BarcodeError(ValueError):
    """The payload cannot be encoded in the requested symbology."""


@dataclass(frozen=True)
class Symbol:
    """A rendered barcode as alternating bar/space widths.

    ``modules`` is a list of ``(is_bar, width)`` pairs in narrow-module units,
    left to right, which a renderer turns into rectangles.
    """

    modules: list[tuple[bool, float]]
    payload: str
    symbology: str
    #: CUPPS ``bcTypeCode`` for this symbology (Table 30.3).
    cupps_type_code: str

    @property
    def total_width(self) -> float:
        """Symbol width in narrow-module units."""
        return sum(width for _, width in self.modules)


def _check_character(payload: str) -> str:
    """Modulo-43 check character (Table 30.3 code ``8``/``7`` variants)."""
    total = sum(CODE39_CHECK_ORDER.index(char) for char in payload)
    return CODE39_CHECK_ORDER[total % 43]


def code39(payload: str, *, add_check_digit: bool = False) -> Symbol:
    """Encode ``payload`` as Code 39.

    With ``add_check_digit`` the symbol carries the modulo-43 check character,
    which corresponds to CUPPS barcode type ``8`` rather than ``3``.
    """
    text = payload.upper()
    invalid = {char for char in text if char not in CODE39_PATTERNS or char == "*"}
    if invalid:
        raise BarcodeError(
            f"Code 39 cannot encode {sorted(invalid)}; valid characters are "
            f"0-9 A-Z and - . space $ / + %"
        )
    if add_check_digit:
        text += _check_character(text)

    modules: list[tuple[bool, float]] = []
    # '*' delimits the symbol at both ends.
    for index, char in enumerate(f"*{text}*"):
        if index:
            # One narrow space between characters.
            modules.append((False, 1.0))
        for position, element in enumerate(CODE39_PATTERNS[char]):
            is_bar = position % 2 == 0
            modules.append((is_bar, WIDE_RATIO if element == "w" else 1.0))

    return Symbol(
        modules=modules,
        payload=text,
        symbology="Code 39",
        cupps_type_code="8" if add_check_digit else "3",
    )


#: Pluggable renderers for symbologies this module does not implement.
_RENDERERS: dict[str, Callable[[str], Symbol]] = {}


def register_renderer(symbology: str, renderer: Callable[[str], Symbol]) -> None:
    """Install an encoder for a symbology such as PDF417 or Aztec.

    A site that prints Resolution 792 boarding passes through a PR device
    registers its own 2D encoder here; the BP and BT AEA paths do not need
    one, because the printer firmware generates the symbol.
    """
    _RENDERERS[symbology.lower()] = renderer


def render(payload: str, symbology: str = "code39") -> Symbol:
    """Encode ``payload`` in ``symbology``.

    Raises :class:`BarcodeError` naming the registration hook when the
    symbology has no encoder, rather than silently printing a symbol that a
    scanner will reject.
    """
    key = symbology.lower().replace("-", "").replace(" ", "")
    if key in ("code39", "3"):
        return code39(payload)
    if key in ("code39check", "8"):
        return code39(payload, add_check_digit=True)
    renderer = _RENDERERS.get(key)
    if renderer is None:
        raise BarcodeError(
            f"no encoder for symbology {symbology!r}. Code 39 is built in; "
            f"register others with cuppsd.barcode.register_renderer(), or "
            f"print through a BP/BT device in AEA mode where the printer "
            f"generates the symbol itself."
        )
    return renderer(payload)


def available_symbologies() -> list[str]:
    """Symbologies that can be rendered right now."""
    return sorted({"code39", "code39check", *_RENDERERS})
