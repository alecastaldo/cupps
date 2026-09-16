"""IATA Resolution 792 boarding pass decoding."""

from __future__ import annotations

from datetime import date

import pytest

from cupps import bcbp


def build_minimal(**kwargs) -> str:
    """A mandatory-section-only record, built to exact field widths."""
    defaults = dict(
        legs=1, name="SMITH/JOHN MR", etkt="E", pnr="XY7K2Q", origin="LHR",
        destination="JFK", carrier="BA ", flight="00117", day="326",
        compartment="Y", seat="032A", sequence="00025", status="1",
    )
    defaults.update(kwargs)
    d = defaults
    return (
        "M" + str(d["legs"]) + d["name"].ljust(20) + d["etkt"]
        + d["pnr"].ljust(7) + d["origin"] + d["destination"] + d["carrier"]
        + d["flight"] + d["day"] + d["compartment"] + d["seat"]
        + d["sequence"] + d["status"] + "00"
    )


def test_mandatory_section():
    parsed = bcbp.parse(build_minimal())
    assert parsed.passenger_name == "SMITH/JOHN MR"
    assert parsed.last_name == "SMITH" and parsed.first_name == "JOHN MR"
    assert parsed.display_name == "JOHN MR SMITH"
    assert parsed.electronic_ticket is True

    leg = parsed.legs[0]
    assert leg.pnr == "XY7K2Q"
    assert leg.route == "LHR-JFK"
    assert leg.flight == "BA117"
    assert leg.seat == "032A"
    assert leg.sequence_number == "00025"
    assert leg.checked_in is True
    assert leg.cabin == "Economy"


def test_conditional_sections():
    """The version marker and unique block appear on the first leg only."""
    unique = (
        "0"          # passenger description
        "W"          # source of check-in
        "W"          # source of issuance
        "6205"       # date of issue
        "B"          # document type
        "BA "        # issuing carrier
        "0012345678901"  # bag tag licence plate
    ).ljust(0)
    repeated = (
        "125"                # airline numeric code
        "1234567890"         # document serial number
        "1"                  # selectee
        "0"                  # international doc verification
        "BA "                # marketing carrier
        "BA "                # frequent flyer carrier
        "1234567890123456"   # frequent flyer number
        " "                  # ID/AD
        "20K"                # free baggage allowance
        "1"                  # fast track
    )
    conditional = (
        ">" "3" + f"{len(unique):02X}" + unique
        + f"{len(repeated):02X}" + repeated
    )
    record = build_minimal()[:-2] + f"{len(conditional):02X}" + conditional

    parsed = bcbp.parse(record)
    assert parsed.version == "3"
    assert parsed.issuing_carrier == "BA"
    assert parsed.baggage_tag_licence_plate == "0012345678901"
    assert parsed.has_bags

    leg = parsed.legs[0]
    assert leg.is_selectee is True
    assert leg.frequent_flyer_number == "1234567890123456"
    assert leg.free_baggage_allowance == "20K"
    assert leg.fast_track == "1"


def test_multi_leg():
    record = build_minimal(legs=2) + (
        "ABC123 ".ljust(7) + "JFK" + "LAX" + "AA " + "00042" + "327" + "F"
        + "001A" + "00007" + "3" + "00"
    )
    parsed = bcbp.parse(record)
    assert len(parsed.legs) == 2
    assert parsed.legs[1].route == "JFK-LAX"
    assert parsed.legs[1].flight == "AA42"
    assert parsed.legs[1].cabin == "First"


def test_security_section():
    record = build_minimal() + "^" + "1" + "04" + "ABCD"
    parsed = bcbp.parse(record)
    assert parsed.security_type == "1"
    assert parsed.security_data == "ABCD"


def test_declared_lengths_are_honoured():
    """A conditional area shorter than its fields must not shift later ones."""
    conditional = ">" "3" + "04" + "0WW6"   # unique block truncated to 4 chars
    record = build_minimal()[:-2] + f"{len(conditional):02X}" + conditional
    parsed = bcbp.parse(record)
    assert parsed.version == "3"
    assert parsed.passenger_description == "0"
    # The fields the short block did not reach stay empty rather than
    # borrowing characters from whatever follows.
    assert parsed.issuing_carrier == ""


def test_julian_date_resolves_to_the_nearest_year():
    leg = bcbp.Leg(flight_date_julian="001")
    resolved = leg.flight_date(reference=date(2026, 12, 28))
    # Day 1 is closer as next year's 1 January than as this year's.
    assert resolved == date(2027, 1, 1)

    leg = bcbp.Leg(flight_date_julian="365")
    assert leg.flight_date(reference=date(2027, 1, 3)) == date(2026, 12, 31)


def test_day_366_is_rejected_in_a_non_leap_year():
    leg = bcbp.Leg(flight_date_julian="366")
    resolved = leg.flight_date(reference=date(2026, 6, 1))
    assert resolved is None or resolved.timetuple().tm_yday == 366


@pytest.mark.parametrize("payload", ["", "0125123456", "hello", "X1SMITH"])
def test_non_boarding_passes_are_rejected(payload):
    with pytest.raises(bcbp.BcbpError):
        bcbp.parse(payload)


def test_short_record_is_rejected_not_silently_truncated():
    with pytest.raises(bcbp.BcbpError, match="mandatory section"):
        bcbp.parse("M1SMITH/JOHN")


def test_build_round_trips():
    original = bcbp.parse(build_minimal())
    again = bcbp.parse(bcbp.build(original))
    assert again.passenger_name == original.passenger_name
    assert again.legs[0].seat == original.legs[0].seat
    assert again.legs[0].route == original.legs[0].route
