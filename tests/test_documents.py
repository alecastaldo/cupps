"""Barcode, PDF and document layout behaviour."""

from __future__ import annotations

import io

import pytest

from cuppsd import barcode, documents
from cuppsd.pdf import MM, Document

pypdf = pytest.importorskip("pypdf")


def test_code39_pattern_table_is_structurally_valid():
    """Every Code 39 character is nine elements with exactly three wide."""
    for char, pattern in barcode.CODE39_PATTERNS.items():
        assert len(pattern) == 9, char
        assert pattern.count("w") == 3, char
        wide_bars = sum(1 for i, e in enumerate(pattern) if e == "w" and i % 2 == 0)
        wide_spaces = 3 - wide_bars
        # Ordinary characters have two wide bars and one wide space; the four
        # symbol characters have three wide spaces and no wide bars.
        assert (wide_bars, wide_spaces) in ((2, 1), (0, 3)), char
    assert len(set(barcode.CODE39_PATTERNS.values())) == len(barcode.CODE39_PATTERNS)


def test_code39_symbol_is_delimited_by_start_and_stop():
    symbol = barcode.code39("0125123456")
    assert symbol.modules[0][0] is True            # begins with a bar
    assert symbol.cupps_type_code == "3"           # Table 30.3
    # 12 characters (10 + two '*' delimiters), 9 elements each, 11 gaps.
    assert len(symbol.modules) == 12 * 9 + 11


def test_code39_check_digit():
    """The modulo-43 check character, giving CUPPS barcode type 8."""
    symbol = barcode.code39("BA1234", add_check_digit=True)
    assert symbol.payload.startswith("BA1234")
    assert len(symbol.payload) == 7
    assert symbol.cupps_type_code == "8"


def test_code39_rejects_characters_it_cannot_encode():
    with pytest.raises(barcode.BarcodeError, match="cannot encode"):
        barcode.code39("abc#def")


def test_unregistered_symbology_names_the_hook():
    with pytest.raises(barcode.BarcodeError, match="register_renderer"):
        barcode.render("data", "pdf417")


def test_a_registered_renderer_is_used():
    sentinel = barcode.code39("TEST")
    barcode.register_renderer("pdf417", lambda payload: sentinel)
    try:
        assert barcode.render("anything", "pdf417") is sentinel
        assert "pdf417" in barcode.available_symbologies()
    finally:
        barcode._RENDERERS.pop("pdf417", None)


def test_generated_pdf_parses():
    document = Document()
    page = document.add_page(210, 99)
    page.text(10 * MM, 80 * MM, "BOARDING PASS", size=14)
    reader = pypdf.PdfReader(io.BytesIO(document.to_bytes()))
    assert len(reader.pages) == 1
    assert "BOARDING PASS" in reader.pages[0].extract_text()
    # 210 x 99 mm in PostScript points.
    assert round(float(reader.pages[0].mediabox.width)) == 595
    assert round(float(reader.pages[0].mediabox.height)) == 281


def test_empty_document_is_refused():
    with pytest.raises(ValueError, match="no pages"):
        Document().to_bytes()


def sample_passenger() -> documents.PassengerDetails:
    return documents.PassengerDetails(
        name="SMITH/JOHN MR", pnr="XY7K2Q", origin="LHR", destination="JFK",
        carrier="BA", flight_number="0117", flight_date="22NOV", gate="B32",
        seat="32A", cabin="Economy", sequence="00025",
    )


def test_boarding_pass_carries_the_key_fields():
    rendered = documents.boarding_pass(sample_passenger(), airline_name="BA")
    text = pypdf.PdfReader(io.BytesIO(rendered.pdf)).pages[0].extract_text()
    for expected in ("SMITH/JOHN MR", "LHR", "JFK", "B32", "32A", "XY7K2Q"):
        assert expected in text, expected


def test_boarding_pass_reports_when_the_2d_symbol_is_missing():
    """The caller must be able to refuse a pass printed without its barcode."""
    rendered = documents.boarding_pass(sample_passenger(), symbology="pdf417")
    assert rendered.barcode_rendered is False
    assert any("register_renderer" in note for note in rendered.notes)


def test_boarding_pass_with_a_registered_encoder_reports_success():
    barcode.register_renderer("pdf417", lambda payload: barcode.code39("TEST"))
    try:
        rendered = documents.boarding_pass(sample_passenger(), symbology="pdf417")
        assert rendered.barcode_rendered is True
        assert rendered.notes == []
    finally:
        barcode._RENDERERS.pop("pdf417", None)


def test_bag_tag_is_resolution_740_sized_and_needs_no_plug_in():
    bag = documents.BagDetails(
        passenger_name="SMITH/JOHN MR", licence_plate="0125123456",
        origin="LHR", destination="JFK", carrier="BA", flight_number="0117",
        flight_date="22NOV", weight_kg=23.4,
    )
    rendered = documents.bag_tag(bag)
    page = pypdf.PdfReader(io.BytesIO(rendered.pdf)).pages[0]
    assert round(float(page.mediabox.width)) == 145   # 51 mm
    assert round(float(page.mediabox.height)) == 1440  # 508 mm
    text = page.extract_text()
    assert "0125123456" in text and "JFK" in text and "23.4 KG" in text


def test_stock_size_prefers_the_device_descriptor():
    from cupps.model import SupportedStock

    stocks = (SupportedStock(stock_name="BP", width=203.0, height=82.0),)
    assert documents.stock_size("BP", stocks) == (203.0, 82.0)
    assert documents.stock_size("BP") == documents.DEFAULT_STOCK_SIZES["BP"]


def test_unknown_stock_is_reported():
    with pytest.raises(KeyError, match="unknown stock"):
        documents.stock_size("NOSUCHSTOCK")
