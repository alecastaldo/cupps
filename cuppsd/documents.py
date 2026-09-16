"""Printable document layouts for a handling application.

These build the PDFs carried in ``<printDocument><pdfPrintDocument>`` for a
Standard Mode PR device (section 30.15), sized to the stock the platform
advertised in ``<supportedStocks>``.

Two notes on what is deliberately *not* here:

* Boarding passes printed through a **BP** device and bag tags through a
  **BT** device use AEA Mode, where the command stream comes from the
  airline's host and the printer's own firmware renders text and barcodes
  (Table 3.3 note 5).  Nothing in this module is involved in that path; see
  :func:`cuppsd.aea.segment`.
* The 2D symbology a Resolution 792 boarding pass needs on paper stock has no
  built-in encoder (see :mod:`cuppsd.barcode`).  When none is registered the
  pass prints with its human-readable fields and the Code 39 sequence
  reference, and :attr:`BoardingPassLayout.barcode_rendered` reports false so
  the caller can refuse to hand it to a passenger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from . import barcode as barcode_module
from .pdf import (
    COURIER,
    COURIER_BOLD,
    HELVETICA,
    HELVETICA_BOLD,
    MM,
    Document,
    Page,
    draw_barcode,
)

#: Stock sizes in millimetres for the common CUPPS stocks. A platform's
#: <supportedStocks> is authoritative; these are the fallbacks used when a
#: stock name is not in the device descriptor.
DEFAULT_STOCK_SIZES = {
    "BP": (210.0, 99.0),    # ATB-style boarding pass
    "BT": (51.0, 508.0),    # IATA Resolution 740 bag tag
    "A4": (210.0, 297.0),
    "LETTER": (215.9, 279.4),
    "RECEIPT": (80.0, 200.0),
}


def stock_size(
    stock_name: str, device_stocks: tuple = ()
) -> tuple[float, float]:
    """Resolve a stock name to ``(width_mm, height_mm)``.

    The device's own ``<supportedStock>`` entry wins, because the platform
    knows what is actually loaded; the table above is only a fallback.
    """
    for stock in device_stocks:
        if stock.stock_name.upper() == stock_name.upper() and stock.width:
            return (stock.width, stock.height)
    size = DEFAULT_STOCK_SIZES.get(stock_name.upper())
    if size is None:
        raise KeyError(
            f"unknown stock {stock_name!r}; the platform did not advertise it "
            f"and it is not one of {sorted(DEFAULT_STOCK_SIZES)}"
        )
    return size


@dataclass
class PassengerDetails:
    """What a handling application needs to print a pass."""

    name: str
    pnr: str = ""
    origin: str = ""
    destination: str = ""
    origin_name: str = ""
    destination_name: str = ""
    carrier: str = ""
    flight_number: str = ""
    flight_date: str = ""
    boarding_time: str = ""
    departure_time: str = ""
    gate: str = ""
    seat: str = ""
    cabin: str = ""
    sequence: str = ""
    frequent_flyer: str = ""
    #: The raw Resolution 792 string to encode in the 2D symbol.
    bcbp: str = ""
    selectee: bool = False
    fast_track: bool = False


@dataclass
class BagDetails:
    """What a handling application needs to print a bag tag."""

    passenger_name: str
    #: IATA Resolution 740 10-digit licence plate number.
    licence_plate: str
    origin: str = ""
    destination: str = ""
    carrier: str = ""
    flight_number: str = ""
    flight_date: str = ""
    weight_kg: Optional[float] = None
    bag_number: int = 1
    bag_count: int = 1
    via: str = ""


@dataclass
class RenderedDocument:
    """A generated document plus what the caller needs to know about it."""

    pdf: bytes
    stock_name: str
    #: False when the 2D boarding-pass symbol could not be generated.
    barcode_rendered: bool = True
    notes: list[str] = field(default_factory=list)


def boarding_pass(
    passenger: PassengerDetails,
    *,
    stock_name: str = "BP",
    device_stocks: tuple = (),
    symbology: str = "pdf417",
    airline_name: str = "",
) -> RenderedDocument:
    """Lay out a boarding pass for a Standard Mode PR device."""
    width, height = stock_size(stock_name, device_stocks)
    document = Document()
    page = document.add_page(width, height)
    notes: list[str] = []

    # The stub occupies the left third; the passenger keeps the right.
    split = page.width * 0.32
    _boarding_pass_stub(page, passenger, 0.0, split, airline_name)
    page.line(split, 0, split, page.height, width=0.4, gray=0.6)
    barcode_ok = _boarding_pass_main(
        page, passenger, split, page.width, airline_name, symbology, notes
    )

    return RenderedDocument(
        pdf=document.to_bytes(),
        stock_name=stock_name,
        barcode_rendered=barcode_ok,
        notes=notes,
    )


def _boarding_pass_stub(
    page: Page,
    passenger: PassengerDetails,
    left: float,
    right: float,
    airline_name: str,
) -> None:
    pad = 4 * MM
    x = left + pad
    top = page.height - pad

    page.text(x, top - 3 * MM, airline_name or passenger.carrier, size=9,
              font=HELVETICA_BOLD)
    page.text(x, top - 8 * MM, "BOARDING PASS", size=6.5, gray=0.35)

    y = top - 16 * MM
    for label, value in (
        ("NAME", passenger.name),
        ("FLIGHT", f"{passenger.carrier}{passenger.flight_number}"),
        ("FROM", passenger.origin),
        ("TO", passenger.destination),
        ("DATE", passenger.flight_date),
    ):
        page.text(x, y, label, size=5.5, gray=0.45)
        page.text(x, y - 4.2 * MM, value, size=8.5, font=HELVETICA_BOLD)
        y -= 10 * MM

    bottom = pad + 12 * MM
    page.text(x, bottom + 6 * MM, "SEAT", size=5.5, gray=0.45)
    page.text(x, bottom, passenger.seat, size=15, font=HELVETICA_BOLD)
    page.text_right(right - pad, bottom, passenger.gate, size=12,
                    font=HELVETICA_BOLD)
    page.text_right(right - pad, bottom + 6 * MM, "GATE", size=5.5, gray=0.45)


def _boarding_pass_main(
    page: Page,
    passenger: PassengerDetails,
    left: float,
    right: float,
    airline_name: str,
    symbology: str,
    notes: list[str],
) -> bool:
    pad = 6 * MM
    x = left + pad
    top = page.height - pad
    inner_right = right - pad

    page.text(x, top - 4 * MM, airline_name or passenger.carrier, size=12,
              font=HELVETICA_BOLD)
    page.text_right(inner_right, top - 4 * MM, "BOARDING PASS", size=9, gray=0.3)
    page.line(x, top - 7 * MM, inner_right, top - 7 * MM, width=0.6)

    # Passenger name and route
    page.text(x, top - 15 * MM, "PASSENGER", size=6, gray=0.45)
    page.text(x, top - 21 * MM, passenger.name.upper(), size=13,
              font=HELVETICA_BOLD)

    route_y = top - 33 * MM
    page.text(x, route_y + 5 * MM, "FROM", size=6, gray=0.45)
    page.text(x, route_y, passenger.origin, size=16, font=HELVETICA_BOLD)
    page.text(x, route_y - 5 * MM, passenger.origin_name, size=7, gray=0.3)

    middle = x + 45 * MM
    page.text(middle, route_y + 5 * MM, "TO", size=6, gray=0.45)
    page.text(middle, route_y, passenger.destination, size=16,
              font=HELVETICA_BOLD)
    page.text(middle, route_y - 5 * MM, passenger.destination_name, size=7,
              gray=0.3)

    # Field grid
    grid_y = route_y - 14 * MM
    columns = [
        ("FLIGHT", f"{passenger.carrier}{passenger.flight_number}"),
        ("DATE", passenger.flight_date),
        ("BOARDING", passenger.boarding_time),
        ("GATE", passenger.gate),
        ("SEAT", passenger.seat),
    ]
    step = (inner_right - x) / len(columns)
    for index, (label, value) in enumerate(columns):
        column_x = x + index * step
        page.text(column_x, grid_y, label, size=6, gray=0.45)
        page.text(column_x, grid_y - 5.5 * MM, value or "-", size=11,
                  font=HELVETICA_BOLD)

    # Secondary line
    detail_y = grid_y - 13 * MM
    details = [f"PNR {passenger.pnr}" if passenger.pnr else ""]
    if passenger.sequence:
        details.append(f"SEQ {passenger.sequence}")
    if passenger.cabin:
        details.append(passenger.cabin.upper())
    if passenger.frequent_flyer:
        details.append(f"FF {passenger.frequent_flyer}")
    page.text(x, detail_y, "   ".join(part for part in details if part), size=8,
              gray=0.2)

    flags = []
    if passenger.selectee:
        flags.append("SSSS")
    if passenger.fast_track:
        flags.append("FAST TRACK")
    if flags:
        page.text_right(inner_right, detail_y, "  ".join(flags), size=9,
                        font=HELVETICA_BOLD)

    # Barcode band
    barcode_y = pad + 4 * MM
    barcode_ok = True
    payload = passenger.bcbp or passenger.pnr
    if payload:
        try:
            symbol = barcode_module.render(payload, symbology)
            draw_barcode(page, symbol, x, barcode_y, height=14 * MM)
        except barcode_module.BarcodeError as exc:
            barcode_ok = False
            notes.append(str(exc))
            # Fall back to a Code 39 reference so the document still carries a
            # scannable identifier, and say plainly that it is not the pass.
            reference = (passenger.sequence or passenger.pnr or "").upper()
            if reference:
                try:
                    draw_barcode(
                        page,
                        barcode_module.code39(reference),
                        x,
                        barcode_y + 4 * MM,
                        height=10 * MM,
                    )
                except barcode_module.BarcodeError:
                    pass
            page.text(
                x,
                barcode_y,
                f"REFERENCE ONLY - {symbology.upper()} SYMBOL NOT AVAILABLE",
                size=6,
                gray=0.4,
            )
    return barcode_ok


def bag_tag(
    bag: BagDetails,
    *,
    stock_name: str = "BT",
    device_stocks: tuple = (),
) -> RenderedDocument:
    """Lay out an IATA Resolution 740 bag tag for a PR device.

    The licence plate number is printed as Code 39, which is the symbology
    baggage systems read, so this path needs no pluggable encoder.
    """
    width, height = stock_size(stock_name, device_stocks)
    document = Document()
    page = document.add_page(width, height)
    notes: list[str] = []

    pad = 3 * MM
    x = pad
    inner_right = page.width - pad
    top = page.height - pad

    page.rect(0, top - 14 * MM, page.width, 14 * MM, gray=0.0, fill=True)
    page.text(x, top - 10 * MM, bag.destination or "???", size=26,
              font=HELVETICA_BOLD, gray=1.0)
    page.text_right(inner_right, top - 10 * MM,
                    f"{bag.carrier}{bag.flight_number}", size=13,
                    font=HELVETICA_BOLD, gray=1.0)

    y = top - 22 * MM
    page.text(x, y, bag.passenger_name.upper()[:28], size=10,
              font=HELVETICA_BOLD)
    y -= 6 * MM
    routing = " / ".join(part for part in (bag.origin, bag.via, bag.destination) if part)
    page.text(x, y, routing, size=9)
    y -= 5 * MM
    page.text(x, y, f"DATE {bag.flight_date}", size=8, gray=0.25)
    if bag.weight_kg is not None:
        page.text_right(inner_right, y, f"{bag.weight_kg:.1f} KG", size=9,
                        font=HELVETICA_BOLD)
    y -= 5 * MM
    page.text(x, y, f"BAG {bag.bag_number} OF {bag.bag_count}", size=8, gray=0.25)

    # Licence plate: human readable plus Code 39, repeated down the tag so it
    # stays readable however the tag wraps around the handle.
    plate = bag.licence_plate.strip()
    symbol = barcode_module.code39(plate)
    band_y = y - 30 * MM
    for _ in range(2):
        if band_y < pad + 20 * MM:
            break
        draw_barcode(page, symbol, x, band_y, height=18 * MM,
                     module_width=0.30 * MM)
        page.text(x, band_y - 5 * MM, plate, size=11, font=COURIER_BOLD)
        band_y -= 55 * MM

    return RenderedDocument(
        pdf=document.to_bytes(), stock_name=stock_name, notes=notes
    )


def itinerary_receipt(
    passenger: PassengerDetails,
    *,
    lines: Optional[list[str]] = None,
    stock_name: str = "A4",
    device_stocks: tuple = (),
    airline_name: str = "",
) -> RenderedDocument:
    """A simple itinerary or excess-baggage receipt."""
    width, height = stock_size(stock_name, device_stocks)
    document = Document()
    page = document.add_page(width, height)

    pad = 20 * MM
    x = pad
    y = page.height - pad

    page.text(x, y, airline_name or passenger.carrier, size=16,
              font=HELVETICA_BOLD)
    page.text_right(page.width - pad, y,
                    datetime.now().strftime("%d %b %Y %H:%M"), size=9, gray=0.35)
    y -= 6 * MM
    page.line(x, y, page.width - pad, y, width=0.8)
    y -= 12 * MM

    page.text(x, y, "PASSENGER ITINERARY RECEIPT", size=11, font=HELVETICA_BOLD)
    y -= 10 * MM

    for label, value in (
        ("Passenger", passenger.name),
        ("Booking reference", passenger.pnr),
        ("Flight", f"{passenger.carrier}{passenger.flight_number}"),
        ("Route", f"{passenger.origin} - {passenger.destination}"),
        ("Date", passenger.flight_date),
        ("Departure", passenger.departure_time),
        ("Seat", passenger.seat),
        ("Cabin", passenger.cabin),
    ):
        if not value.strip():
            continue
        page.text(x, y, f"{label}:", size=9, gray=0.4)
        page.text(x + 45 * MM, y, value, size=9, font=HELVETICA_BOLD)
        y -= 6 * MM

    if lines:
        y -= 6 * MM
        page.line(x, y, page.width - pad, y, width=0.4, gray=0.6)
        y -= 8 * MM
        for line in lines:
            page.text(x, y, line, size=9, font=COURIER)
            y -= 5 * MM

    return RenderedDocument(pdf=document.to_bytes(), stock_name=stock_name)
