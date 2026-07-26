"""Tests for schedule-of-investments extraction."""

from __future__ import annotations

import pytest

from ncsr.holdings import extract_holdings, parse_legend, reconcile, stated_total
from ncsr.htmltables import parse
from ncsr.pipeline import analyze

from fixtures import BY_LABEL, load


@pytest.fixture(scope="module")
def penn():
    document, header = load(BY_LABEL["penn"])
    analysis = analyze(document, header)
    return analysis, parse(document)


def _schedule(analysis, fund_name):
    by_name = {v: k for k, v in analysis.header.series.items()}
    series_id = by_name[fund_name]
    sections = [
        s
        for s in analysis.sections
        if s.section_type == "schedule_of_investments" and s.series_ids == (series_id,)
    ]
    return series_id, max(sections, key=lambda s: s.end - s.start)


def test_stated_total_ignores_the_cost_parenthetical():
    """"TOTAL INVESTMENTS - 98.6% (Cost $117,939,535) $ 121,162,999" states both
    cost and market value; for an equity fund they differ by more than 2x."""
    text = "TOTAL INVESTMENTS — 98.6% (Cost $117,939,535) $ 121,162,999 Other Assets"
    assert stated_total(text) == 121162999.0


def test_holdings_reconcile_to_the_schedules_own_total(penn):
    """The filing states its own total, so extraction is self-checking."""
    analysis, parsed = penn
    series_id, section = _schedule(analysis, "High Yield Bond Fund")
    holdings = extract_holdings(parsed, series_id, section.start, section.end)
    extracted, stated, difference = reconcile(
        holdings, parsed.text[section.start : section.end]
    )
    assert stated == 121162999.0
    assert difference < 0.001, f"extracted {extracted:,.0f} vs stated {stated:,.0f}"


def test_fair_value_hierarchy_table_is_not_read_as_holdings(penn):
    """Its rows are asset-class totals ("Corporate Bonds 114,962,448"); reading
    them as securities roughly doubles the fund."""
    analysis, parsed = penn
    series_id, section = _schedule(analysis, "High Yield Bond Fund")
    holdings = extract_holdings(parsed, series_id, section.start, section.end)
    issuers = {h.issuer for h in holdings}
    assert "Corporate Bonds" not in issuers
    assert "Loan Agreements" not in issuers


def test_bond_terms_are_split_out(penn):
    analysis, parsed = penn
    series_id, section = _schedule(analysis, "High Yield Bond Fund")
    holdings = extract_holdings(parsed, series_id, section.start, section.end)
    with_terms = [h for h in holdings if h.coupon and h.maturity_date]
    assert len(with_terms) > 50
    sample = with_terms[0]
    assert "%" not in sample.issuer
    assert "/" in sample.maturity_date


def test_categories_are_carried_onto_holdings(penn):
    analysis, parsed = penn
    series_id, section = _schedule(analysis, "High Yield Bond Fund")
    holdings = extract_holdings(parsed, series_id, section.start, section.end)
    assert len({h.category for h in holdings if h.category}) > 5


def test_legend_is_read_from_the_filings_own_wording():
    legend = parse_legend(
        "* Non-income producing security. @ Security exempt from registration "
        "under Rule 144A of the Securities Act of 1933. # Restricted Security."
    )
    assert legend.get("*") == "non_income_producing"
    assert legend.get("@") == "rule_144a"
    assert legend.get("#") == "restricted"


def test_legend_symbols_are_local_to_the_filing():
    """Nothing may assume a global symbol table: the same mark means different
    things in different filings, and in different sections of one filing."""
    a = parse_legend(
        "(1) The value of this security was determined using significant "
        "unobservable inputs and is reported as a Level 3 security."
    )
    b = parse_legend(
        "(1) Includes internally fair valued securities currently priced at zero."
    )
    assert a.get("(1)") == "level_3"
    assert "(1)" not in b


def test_holdings_offsets_round_trip(penn):
    analysis, parsed = penn
    series_id, section = _schedule(analysis, "High Yield Bond Fund")
    for holding in extract_holdings(parsed, series_id, section.start, section.end)[:25]:
        source = parsed.text[holding.char_start : holding.char_end]
        assert "".join(c for c in source if c.isdigit())


def test_div_layout_filings_yield_no_holdings():
    for label in ("gugg", "blackrock", "victory"):
        document, header = load(BY_LABEL[label])
        analysis = analyze(document, header)
        parsed = parse(document)
        found = []
        for section in analysis.sections:
            if section.section_type == "schedule_of_investments" and len(section.series_ids) == 1:
                found += extract_holdings(
                    parsed, section.series_ids[0], section.start, section.end
                )
        assert found == []


def test_flagged_holdings_reconcile_to_the_filings_stated_144a_total(penn):
    """Independent check on the whole legend -> flag -> row chain. Penn states
    "the aggregate value of Rule 144A securities was $99,518,726, which
    represents 81.0% of the Fund's net assets"."""
    analysis, parsed = penn
    series_id, section = _schedule(analysis, "High Yield Bond Fund")
    holdings = extract_holdings(parsed, series_id, section.start, section.end)
    total = sum(
        float(h.value) for h in holdings if h.value and "rule_144a" in h.flags
    )
    assert total == 99518726.0
    assert round(100 * total / 122897622.0, 1) == 81.0
