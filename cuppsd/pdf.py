"""A minimal PDF writer for CUPPS Standard Mode print documents.

``<printDocument><pdfPrintDocument>`` carries a Base64 PDF (Listing 30.41), so
an application printing through a PR device has to produce one.  This writer
covers what a boarding pass, bag tag or receipt needs -- text, rules, filled
boxes and barcode bars -- with no external dependency, because CUPPS
workstation images are locked down and cannot be assumed to carry a
PDF toolkit.

Coordinates are in PostScript points (1/72 inch) with the origin at the
bottom-left of the page, which is the PDF convention.  The layout helpers in
``documents.py`` work in millimetres and convert, matching the standard stock
coordinate system of section 26.14.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

#: PostScript points per millimetre.
MM = 72.0 / 25.4

#: The 14 standard PDF fonts need no embedding, which keeps documents small
#: and guarantees they render on any spooler.
HELVETICA = "Helvetica"
HELVETICA_BOLD = "Helvetica-Bold"
COURIER = "Courier"
COURIER_BOLD = "Courier-Bold"

_FONT_RESOURCE = {
    HELVETICA: "F1",
    HELVETICA_BOLD: "F2",
    COURIER: "F3",
    COURIER_BOLD: "F4",
}


def _escape(text: str) -> str:
    """Escape a string for a PDF literal-string operand."""
    return (
        text.replace("\\", r"\\")
        .replace("(", r"\(")
        .replace(")", r"\)")
        .replace("\r", "")
    )


def _number(value: float) -> str:
    """Format a coordinate compactly without exponent notation."""
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


@dataclass
class Page:
    """One page of a document, accumulating content-stream operators."""

    width: float
    height: float
    _operators: list[str] = field(default_factory=list, repr=False)

    def text(
        self,
        x: float,
        y: float,
        value: str,
        *,
        size: float = 10.0,
        font: str = HELVETICA,
        gray: float = 0.0,
    ) -> "Page":
        """Draw a single line of text with its baseline at ``(x, y)``."""
        if not value:
            return self
        resource = _FONT_RESOURCE.get(font, "F1")
        self._operators.append(
            f"BT {_number(gray)} g /{resource} {_number(size)} Tf "
            f"{_number(x)} {_number(y)} Td ({_escape(value)}) Tj ET"
        )
        return self

    def text_right(
        self,
        x: float,
        y: float,
        value: str,
        *,
        size: float = 10.0,
        font: str = HELVETICA,
        gray: float = 0.0,
    ) -> "Page":
        """Draw text ending at ``x`` instead of starting there."""
        return self.text(
            x - text_width(value, size, font), y, value, size=size, font=font, gray=gray
        )

    def lines(
        self,
        x: float,
        y: float,
        values: Iterable[str],
        *,
        size: float = 10.0,
        leading: float = 0.0,
        font: str = HELVETICA,
    ) -> "Page":
        """Draw successive lines downward from ``(x, y)``."""
        step = leading or size * 1.2
        for index, value in enumerate(values):
            self.text(x, y - index * step, value, size=size, font=font)
        return self

    def rect(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        *,
        gray: float = 0.0,
        fill: bool = True,
    ) -> "Page":
        operator = "f" if fill else "S"
        self._operators.append(
            f"{_number(gray)} {'g' if fill else 'G'} "
            f"{_number(x)} {_number(y)} {_number(width)} {_number(height)} re {operator}"
        )
        return self

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        *,
        width: float = 0.5,
        gray: float = 0.0,
    ) -> "Page":
        self._operators.append(
            f"{_number(gray)} G {_number(width)} w "
            f"{_number(x1)} {_number(y1)} m {_number(x2)} {_number(y2)} l S"
        )
        return self

    def content(self) -> bytes:
        return "\n".join(self._operators).encode("latin-1", "replace")


def text_width(value: str, size: float, font: str = HELVETICA) -> float:
    """Approximate the rendered width of ``value``.

    Courier is monospaced at exactly 0.6 em.  For the Helvetica faces this
    uses an average advance rather than the real metrics: good enough for
    right-aligning and centring fields on a pass, and it never needs the AFM
    tables that would otherwise have to ship alongside.
    """
    if font in (COURIER, COURIER_BOLD):
        return len(value) * size * 0.6
    factor = 0.55 if font == HELVETICA_BOLD else 0.5
    return len(value) * size * factor


class Document:
    """A small multi-page PDF."""

    def __init__(self) -> None:
        self.pages: list[Page] = []

    def add_page(self, width_mm: float, height_mm: float) -> Page:
        """Add a page sized in millimetres and return it for drawing."""
        page = Page(width=width_mm * MM, height=height_mm * MM)
        self.pages.append(page)
        return page

    def to_bytes(self) -> bytes:
        """Serialise to a complete PDF file."""
        if not self.pages:
            raise ValueError("document has no pages")

        objects: list[bytes] = []

        def add_object(payload: bytes) -> int:
            objects.append(payload)
            return len(objects)  # object numbers are 1-based

        font_objects = {
            resource: add_object(
                b"<< /Type /Font /Subtype /Type1 /BaseFont /"
                + name.encode("ascii")
                + b" /Encoding /WinAnsiEncoding >>"
            )
            for name, resource in _FONT_RESOURCE.items()
        }
        resources = (
            b"<< /Font << "
            + b" ".join(
                b"/" + resource.encode("ascii") + b" " + str(number).encode("ascii") + b" 0 R"
                for resource, number in font_objects.items()
            )
            + b" >> >>"
        )

        # The pages object must know its kids, which are not numbered yet, so
        # reserve its slot and fill it in once the page objects exist.
        pages_number = add_object(b"")
        page_numbers: list[int] = []
        for page in self.pages:
            stream = page.content()
            content_number = add_object(
                b"<< /Length "
                + str(len(stream)).encode("ascii")
                + b" >>\nstream\n"
                + stream
                + b"\nendstream"
            )
            page_numbers.append(
                add_object(
                    b"<< /Type /Page /Parent "
                    + str(pages_number).encode("ascii")
                    + b" 0 R /MediaBox [0 0 "
                    + _number(page.width).encode("ascii")
                    + b" "
                    + _number(page.height).encode("ascii")
                    + b"] /Resources "
                    + resources
                    + b" /Contents "
                    + str(content_number).encode("ascii")
                    + b" 0 R >>"
                )
            )

        objects[pages_number - 1] = (
            b"<< /Type /Pages /Count "
            + str(len(page_numbers)).encode("ascii")
            + b" /Kids ["
            + b" ".join(str(n).encode("ascii") + b" 0 R" for n in page_numbers)
            + b"] >>"
        )
        catalog_number = add_object(
            b"<< /Type /Catalog /Pages " + str(pages_number).encode("ascii") + b" 0 R >>"
        )

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for number, payload in enumerate(objects, start=1):
            offsets.append(len(out))
            out += str(number).encode("ascii") + b" 0 obj\n" + payload + b"\nendobj\n"

        xref_offset = len(out)
        out += b"xref\n0 " + str(len(objects) + 1).encode("ascii") + b"\n"
        out += b"0000000000 65535 f \n"
        for offset in offsets[1:]:
            out += f"{offset:010d} 00000 n \n".encode("ascii")
        out += (
            b"trailer\n<< /Size "
            + str(len(objects) + 1).encode("ascii")
            + b" /Root "
            + str(catalog_number).encode("ascii")
            + b" 0 R >>\nstartxref\n"
            + str(xref_offset).encode("ascii")
            + b"\n%%EOF\n"
        )
        return bytes(out)


def draw_barcode(
    page: Page,
    symbol,
    x: float,
    y: float,
    *,
    height: float,
    module_width: float = 0.33 * MM,
) -> float:
    """Draw a :class:`~cuppsd.barcode.Symbol` and return its width in points.

    ``module_width`` is the narrow-element width; 0.33 mm is a common default
    that scans reliably on thermal boarding-pass and bag-tag stock.
    """
    cursor = x
    for is_bar, width in symbol.modules:
        span = width * module_width
        if is_bar:
            page.rect(cursor, y, span, height, gray=0.0, fill=True)
        cursor += span
    return cursor - x
