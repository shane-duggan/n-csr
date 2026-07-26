"""Tests for offset-preserving HTML parsing and statement extraction."""

from __future__ import annotations

import pytest

from ncsr.htmltables import parse
from ncsr.normalize import textify
from ncsr.pipeline import analyze
from ncsr.statements import (
    STATEMENT_SECTIONS,
    extract_line_items,
    normalize_value,
)

from fixtures import FIXTURES, BY_LABEL, load


@pytest.fixture(scope="module")
def penn():
    document, header = load(BY_LABEL["penn"])
    analysis = analyze(document, header)
    return analysis, parse(document)


# --------------------------------------------------------------------------
# the invariant everything else rests on
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.label)
def test_parsed_text_is_identical_to_textify(fixture):
    """Table offsets live in the same coordinate space as section offsets and
    archived evidence. If these two ever diverge, every stored offset silently
    points at the wrong text."""
    document, _ = load(fixture)
    assert parse(document).text == textify(document)


def test_tables_are_within_document_bounds():
    document, _ = load(BY_LABEL["penn"])
    parsed = parse(document)
    assert parsed.tables
    for table in parsed.tables:
        assert 0 <= table.start <= table.end <= len(parsed.text)


def test_cells_resolve_to_their_own_text():
    document, _ = load(BY_LABEL["imst"])
    parsed = parse(document)
    checked = 0
    for table in parsed.tables[:20]:
        for row in table.rows:
            for cell in row.cells:
                assert table.start <= cell.start <= cell.end <= table.end
                checked += 1
    assert checked


# --------------------------------------------------------------------------
# value normalization
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("$ 822,559", "822559"),
        ("16,634", "16634"),
        ("(32,260)", "-32260"),
        ("1.25", "1.25"),
        ("", None),
        ("Dividends", None),
        ("—", None),
    ],
)
def test_normalize_value(raw, expected):
    assert normalize_value(raw) == expected


# --------------------------------------------------------------------------
# statement extraction
# --------------------------------------------------------------------------

def _line_items(analysis, parsed):
    items = []
    for section in analysis.sections:
        if section.section_type in STATEMENT_SECTIONS:
            items += extract_line_items(
                parsed, analysis.header.series, section.start, section.end,
                section.series_ids,
            )
    return items


def test_dividend_income_matches_the_filing(penn):
    """Golden value read by hand from Penn Series' Statement of Operations:
    Money Market Fund, dividends, year ended 2025-12-31."""
    analysis, parsed = penn
    by_name = {v: k for k, v in analysis.header.series.items()}
    series_id = by_name["Money Market Fund"]

    items = _line_items(analysis, parsed)
    dividends = [
        i for i in items if i.series_id == series_id and i.caption == "Dividends"
    ]
    assert len(dividends) == 1
    assert dividends[0].value == "822559"


def test_related_captions_also_match(penn):
    """Guards against a column being off by one, which a single figure would
    not catch."""
    analysis, parsed = penn
    by_name = {v: k for k, v in analysis.header.series.items()}
    series_id = by_name["Money Market Fund"]
    items = {
        i.caption: i.value for i in _line_items(analysis, parsed)
        if i.series_id == series_id
    }
    assert items["Interest"] == "4431517"
    assert items["Total Investment Income"] == "5254076"


def test_line_item_offsets_round_trip(penn):
    analysis, parsed = penn
    for item in _line_items(analysis, parsed)[:40]:
        source = parsed.text[item.char_start : item.char_end]
        digits = "".join(c for c in source if c.isdigit())
        assert digits, f"empty source for {item.caption}"
        assert digits in item.value.replace("-", "").replace(".", "")


def test_columnar_statement_maps_each_fund_to_its_own_column(penn):
    """Penn reports four funds side by side; each must get its own values."""
    analysis, parsed = penn
    dividends = {
        i.series_id: i.value
        for i in _line_items(analysis, parsed)
        if i.caption == "Dividends"
    }
    assert len(dividends) == len(analysis.header.series) == 29
    assert len(set(dividends.values())) > 20  # not all one column repeated


def test_single_fund_sections_use_section_attribution():
    """Guardian names the fund in a banner, not the table header."""
    document, header = load(BY_LABEL["guard"])
    analysis = analyze(document, header)
    items = _line_items(analysis, parse(document))
    assert items
    assert {i.series_id for i in items} <= set(analysis.header.series)


def test_div_layout_filings_yield_nothing_rather_than_wrong_rows():
    """Four filings lay financial data out in <div> rather than <table>.
    Extraction must decline rather than invent rows -- see README."""
    for label in ("gugg", "templeton", "blackrock", "victory"):
        document, header = load(BY_LABEL[label])
        analysis = analyze(document, header)
        assert _line_items(analysis, parse(document)) == []
