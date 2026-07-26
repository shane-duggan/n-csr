"""Tests for fair-value hierarchy extraction."""

from __future__ import annotations

import pytest

from ncsr.fairvalue import amount, extract_hierarchy
from ncsr.htmltables import parse
from ncsr.pipeline import analyze

from fixtures import BY_LABEL, load


@pytest.fixture(scope="module")
def penn():
    document, header = load(BY_LABEL["penn"])
    return analyze(document, header), parse(document)


def _hierarchy(analysis, parsed, fund_name):
    by_name = {v: k for k, v in analysis.header.series.items()}
    series_id = by_name[fund_name]
    section = max(
        (
            s
            for s in analysis.sections
            if s.section_type == "schedule_of_investments"
            and s.series_ids == (series_id,)
        ),
        key=lambda s: s.end - s.start,
    )
    return extract_hierarchy(parsed, series_id, section.start, section.end)


@pytest.mark.parametrize(
    "cell,expected",
    [
        ("$ 605,497", "605497"),
        ("—", "0"),        # a dash is a disclosed zero, not a missing value
        ("$—", "0"),
        ("— (1)", "0"),    # trailing footnote marker
        ("", None),        # genuinely absent
        ("Corporate Bonds", None),
    ],
)
def test_amount_treats_a_dash_as_a_disclosed_zero(cell, expected):
    assert amount(cell) == expected


def test_levels_sum_to_the_stated_total(penn):
    """The table's own arithmetic is the check: a misread column shows up as a
    discrepancy rather than being silently believed."""
    analysis, parsed = penn
    hierarchy = _hierarchy(analysis, parsed, "High Yield Bond Fund")
    assert hierarchy.is_consistent, hierarchy.discrepancies
    assert (
        hierarchy.total_at(1) + hierarchy.total_at(2) + hierarchy.total_at(3)
        == 121162999.0
    )


def test_zero_level_3_is_recorded_as_an_affirmation(penn):
    analysis, parsed = penn
    hierarchy = _hierarchy(analysis, parsed, "High Yield Bond Fund")
    assert hierarchy.total_at(3) == 0.0
    level_3 = [a for a in hierarchy.amounts if a.level == 3]
    assert level_3, "the fund disclosed Level 3 amounts; they must be recorded"
    assert all(a.amount == "0" for a in level_3)


def test_non_zero_level_3_is_extracted(penn):
    """Penn's Large Growth Stock Fund holds Level 3 preferred stocks."""
    analysis, parsed = penn
    hierarchy = _hierarchy(analysis, parsed, "Large Growth Stock Fund")
    assert hierarchy.is_consistent
    assert hierarchy.total_at(3) == 579833.0
    categories = {
        a.category for a in hierarchy.amounts
        if a.level == 3 and float(a.amount) > 0 and not a.is_total_row
    }
    assert categories == {"Preferred Stocks"}


def test_grand_total_wins_over_a_subtotal(penn):
    """A hierarchy table may carry several rows beginning "Total"; picking the
    first would report an asset-class subtotal as the fund-wide figure."""
    analysis, parsed = penn
    hierarchy = _hierarchy(analysis, parsed, "Large Growth Stock Fund")
    totals = [a for a in hierarchy.amounts if a.is_total_row and a.level == 3]
    assert len(totals) > 1, "expected a subtotal as well as a grand total"
    assert hierarchy.total_at(3) == 579833.0


def test_every_hierarchy_in_the_corpus_is_self_consistent():
    """Corpus-wide guard: no filing may produce a table whose levels do not sum
    to its stated totals."""
    inconsistent = []
    for label in ("penn", "guard", "voya", "consolidated", "master", "semiannual"):
        document, header = load(BY_LABEL[label])
        analysis = analyze(document, header)
        parsed = parse(document)
        for section in analysis.sections:
            if section.section_type != "schedule_of_investments":
                continue
            if len(section.series_ids) != 1:
                continue
            hierarchy = extract_hierarchy(
                parsed, section.series_ids[0], section.start, section.end
            )
            if hierarchy and not hierarchy.is_consistent:
                inconsistent.append((label, hierarchy.discrepancies))
    assert not inconsistent
