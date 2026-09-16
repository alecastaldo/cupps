"""IATA Resolution 792 Bar Coded Boarding Pass decoding.

CUPPS presents barcode payloads as plain strings and makes parsing the
application's job (section 30.9.1 note).  A boarding gate or check-in
application scanning a BC device gets a Resolution 792 "M" record, which this
module turns into structured data.

The format is a fixed mandatory section followed by per-leg repeated sections,
each carrying a declared-length conditional area.  Every length in the record
is honoured rather than assumed, so a truncated or vendor-padded scan yields
whatever fields are genuinely present instead of raising or, worse, silently
shifting every subsequent field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

#: Compartment codes seen in the wild map to cabin names for display only;
#: the code itself is authoritative.
COMPARTMENT_NAMES = {
    "F": "First",
    "A": "First (discounted)",
    "J": "Business",
    "C": "Business",
    "D": "Business (discounted)",
    "I": "Business (discounted)",
    "Z": "Business (discounted)",
    "W": "Premium Economy",
    "S": "Economy",
    "Y": "Economy",
    "B": "Economy",
    "H": "Economy",
    "K": "Economy",
    "L": "Economy",
    "M": "Economy",
    "N": "Economy",
    "Q": "Economy",
    "T": "Economy",
    "V": "Economy",
    "X": "Economy",
    "G": "Economy",
    "U": "Economy",
    "E": "Economy",
    "O": "Economy",
    "R": "Economy",
}

#: Passenger status codes (Resolution 792 mandatory item 113).
PASSENGER_STATUS = {
    "0": "Ticket issued, passenger not checked in",
    "1": "Ticket issued, passenger checked in",
    "2": "Baggage checked, passenger not checked in",
    "3": "Baggage checked, passenger checked in",
    "4": "Passenger passed security",
    "5": "Passenger passed gate",
    "6": "Transit",
    "7": "Standby",
    "8": "Boarding pass revalidation done",
    "9": "Original seat not taken",
}


class BcbpError(ValueError):
    """The scanned data is not a decodable Resolution 792 record."""


class _Cursor:
    """A bounded reader over the barcode string."""

    def __init__(self, data: str) -> None:
        self._data = data
        self._pos = 0

    @property
    def remaining(self) -> int:
        return len(self._data) - self._pos

    def take(self, count: int) -> str:
        """Consume up to ``count`` characters, stripping trailing padding."""
        chunk = self._data[self._pos : self._pos + count]
        self._pos += len(chunk)
        return chunk.strip()

    def take_raw(self, count: int) -> str:
        chunk = self._data[self._pos : self._pos + count]
        self._pos += len(chunk)
        return chunk

    def peek(self, count: int = 1) -> str:
        return self._data[self._pos : self._pos + count]

    def take_hex(self, count: int = 2) -> int:
        """Consume a hexadecimal length field, tolerating blanks as zero."""
        raw = self.take_raw(count).strip()
        if not raw:
            return 0
        try:
            return int(raw, 16)
        except ValueError:
            return 0

    def sub(self, length: int) -> "_Cursor":
        """Consume ``length`` characters and return a cursor over just them."""
        return _Cursor(self.take_raw(length))


@dataclass
class Leg:
    """One flight leg of a boarding pass."""

    pnr: str = ""
    origin: str = ""
    destination: str = ""
    operating_carrier: str = ""
    flight_number: str = ""
    flight_date_julian: str = ""
    compartment: str = ""
    seat: str = ""
    sequence_number: str = ""
    passenger_status: str = ""

    # Conditional, repeated section
    airline_numeric_code: str = ""
    document_serial_number: str = ""
    selectee_indicator: str = ""
    international_doc_verification: str = ""
    marketing_carrier: str = ""
    frequent_flyer_carrier: str = ""
    frequent_flyer_number: str = ""
    id_ad_indicator: str = ""
    free_baggage_allowance: str = ""
    fast_track: str = ""
    airline_individual_use: str = ""

    @property
    def flight(self) -> str:
        """Carrier and flight number with leading zeros removed."""
        number = self.flight_number.lstrip("0").strip()
        # The last character of the 5-byte field is an optional suffix.
        return f"{self.operating_carrier}{number}".strip()

    @property
    def route(self) -> str:
        return f"{self.origin}-{self.destination}"

    @property
    def cabin(self) -> str:
        return COMPARTMENT_NAMES.get(self.compartment, self.compartment)

    @property
    def status_text(self) -> str:
        return PASSENGER_STATUS.get(self.passenger_status, self.passenger_status)

    @property
    def checked_in(self) -> bool:
        return self.passenger_status in ("1", "3", "4", "5")

    @property
    def is_selectee(self) -> bool:
        """True when the record flags the passenger for secondary screening."""
        return self.selectee_indicator == "1"

    def flight_date(self, reference: Optional[date] = None) -> Optional[date]:
        """Resolve the 3-digit Julian day to a calendar date.

        The record carries no year, so the nearest year to ``reference``
        (today by default) is chosen: a day-of-year more than about six months
        ahead is read as last year, and one far behind as next year.  That
        handles both year-end rollover and passes scanned slightly early.
        """
        raw = self.flight_date_julian.strip()
        if not raw.isdigit():
            return None
        day_of_year = int(raw)
        if not 1 <= day_of_year <= 366:
            return None
        today = reference or date.today()
        best: Optional[date] = None
        for year in (today.year - 1, today.year, today.year + 1):
            try:
                candidate = date(year, 1, 1) + timedelta(days=day_of_year - 1)
            except (ValueError, OverflowError):
                continue
            if candidate.timetuple().tm_yday != day_of_year:
                # 366 in a non-leap year rolls into the next year; reject it.
                continue
            if best is None or abs((candidate - today).days) < abs((best - today).days):
                best = candidate
        return best


@dataclass
class BoardingPass:
    """A decoded Resolution 792 boarding pass."""

    passenger_name: str = ""
    electronic_ticket: bool = False
    legs: list[Leg] = field(default_factory=list)

    # Conditional, unique section
    version: str = ""
    passenger_description: str = ""
    source_of_checkin: str = ""
    source_of_issuance: str = ""
    date_of_issue_julian: str = ""
    document_type: str = ""
    issuing_carrier: str = ""
    baggage_tag_licence_plate: str = ""
    baggage_tag_2: str = ""
    baggage_tag_3: str = ""

    # Security section
    security_type: str = ""
    security_data: str = ""

    raw: str = ""

    @property
    def last_name(self) -> str:
        return self.passenger_name.split("/", 1)[0].strip()

    @property
    def first_name(self) -> str:
        parts = self.passenger_name.split("/", 1)
        return parts[1].strip() if len(parts) > 1 else ""

    @property
    def display_name(self) -> str:
        """``FIRST LAST`` for on-screen display."""
        first, last = self.first_name, self.last_name
        return f"{first} {last}".strip() if first else last

    @property
    def first_leg(self) -> Optional[Leg]:
        return self.legs[0] if self.legs else None

    @property
    def has_bags(self) -> bool:
        return bool(self.baggage_tag_licence_plate.strip())


def parse(data: str) -> BoardingPass:
    """Decode a Resolution 792 "M" record.

    Raises :class:`BcbpError` when the data is not a boarding pass at all, so
    a caller scanning a mixed stream of barcodes (bag tags, documents) can
    distinguish "not a boarding pass" from "a broken boarding pass".
    """
    if not data:
        raise BcbpError("empty barcode data")
    text = data.strip()
    if not text.startswith("M"):
        raise BcbpError(
            f"not a Resolution 792 record: expected format code 'M', "
            f"got {text[:1]!r}"
        )
    if len(text) < 60:
        raise BcbpError(
            f"record is {len(text)} characters; the mandatory section alone "
            f"needs 60"
        )

    cursor = _Cursor(text)
    cursor.take(1)  # format code
    raw_legs = cursor.take(1)
    try:
        leg_count = int(raw_legs)
    except ValueError as exc:
        raise BcbpError(f"leg count {raw_legs!r} is not a digit") from exc
    if not 1 <= leg_count <= 9:
        raise BcbpError(f"leg count {leg_count} is out of range 1-9")

    boarding_pass = BoardingPass(raw=text)
    boarding_pass.passenger_name = cursor.take(20)
    boarding_pass.electronic_ticket = cursor.take(1) == "E"

    for leg_index in range(leg_count):
        if cursor.remaining <= 0:
            break
        leg = Leg()
        leg.pnr = cursor.take(7)
        leg.origin = cursor.take(3)
        leg.destination = cursor.take(3)
        leg.operating_carrier = cursor.take(3)
        leg.flight_number = cursor.take(5)
        leg.flight_date_julian = cursor.take(3)
        leg.compartment = cursor.take(1)
        leg.seat = cursor.take(4)
        leg.sequence_number = cursor.take(5)
        leg.passenger_status = cursor.take(1)

        conditional_size = cursor.take_hex(2)
        if conditional_size:
            conditional = cursor.sub(conditional_size)
            _parse_conditional(
                conditional,
                boarding_pass,
                leg,
                is_first_leg=(leg_index == 0),
            )
        boarding_pass.legs.append(leg)

    # The security section, when present, follows the final leg.
    if cursor.peek(1) == "^":
        cursor.take_raw(1)
        boarding_pass.security_type = cursor.take(1)
        length = cursor.take_hex(2)
        boarding_pass.security_data = cursor.take_raw(length)

    return boarding_pass


def _parse_conditional(
    cursor: _Cursor,
    boarding_pass: BoardingPass,
    leg: Leg,
    *,
    is_first_leg: bool,
) -> None:
    """Parse one leg's conditional area.

    The version marker and unique structured message appear only on the first
    leg; every leg carries the repeated structured message and may carry a
    trailing airline-private area.
    """
    if is_first_leg and cursor.peek(1) == ">":
        cursor.take_raw(1)
        boarding_pass.version = cursor.take(1)
        unique_size = cursor.take_hex(2)
        if unique_size:
            unique = cursor.sub(unique_size)
            boarding_pass.passenger_description = unique.take(1)
            boarding_pass.source_of_checkin = unique.take(1)
            boarding_pass.source_of_issuance = unique.take(1)
            boarding_pass.date_of_issue_julian = unique.take(4)
            boarding_pass.document_type = unique.take(1)
            boarding_pass.issuing_carrier = unique.take(3)
            boarding_pass.baggage_tag_licence_plate = unique.take(13)
            boarding_pass.baggage_tag_2 = unique.take(13)
            boarding_pass.baggage_tag_3 = unique.take(13)

    repeated_size = cursor.take_hex(2)
    if repeated_size:
        repeated = cursor.sub(repeated_size)
        leg.airline_numeric_code = repeated.take(3)
        leg.document_serial_number = repeated.take(10)
        leg.selectee_indicator = repeated.take(1)
        leg.international_doc_verification = repeated.take(1)
        leg.marketing_carrier = repeated.take(3)
        leg.frequent_flyer_carrier = repeated.take(3)
        leg.frequent_flyer_number = repeated.take(16)
        leg.id_ad_indicator = repeated.take(1)
        leg.free_baggage_allowance = repeated.take(3)
        leg.fast_track = repeated.take(1)

    # Whatever remains in the conditional area is for individual airline use.
    if cursor.remaining > 0:
        leg.airline_individual_use = cursor.take_raw(cursor.remaining).strip()


def build(boarding_pass: BoardingPass) -> str:
    """Encode a minimal mandatory-section "M" record.

    Used by the platform simulator and by tests to produce scannable data;
    only the mandatory items are emitted, which every reader accepts.
    """
    legs = boarding_pass.legs or [Leg()]
    parts = [
        "M",
        str(len(legs)),
        boarding_pass.passenger_name[:20].ljust(20),
        "E" if boarding_pass.electronic_ticket else " ",
    ]
    for leg in legs:
        parts.extend(
            [
                leg.pnr[:7].ljust(7),
                leg.origin[:3].ljust(3),
                leg.destination[:3].ljust(3),
                leg.operating_carrier[:3].ljust(3),
                leg.flight_number[:5].rjust(5, "0"),
                leg.flight_date_julian[:3].rjust(3, "0"),
                leg.compartment[:1] or "Y",
                leg.seat[:4].ljust(4),
                leg.sequence_number[:5].rjust(5, "0"),
                leg.passenger_status[:1] or "0",
                "00",
            ]
        )
    return "".join(parts)
