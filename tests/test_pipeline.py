"""Regression tests against the 15-filing corpus.

These are integration tests over real EDGAR documents. The first run downloads
and caches ~250 MB; later runs are offline.
"""

from __future__ import annotations

import pytest

from ncsr.audit import find_opinions, reconcile_coverage
from ncsr.header import parse_header
from ncsr.normalize import fund_key, textify, unescape_stable
from ncsr.pipeline import FilingKind, analyze
from ncsr.sectioner import find_item7_spans

from fixtures import FIXTURES, BY_LABEL, load

ANNUAL = [f for f in FIXTURES if f.kind == "open_end_annual"]


@pytest.fixture(scope="session")
def analyses():
    """Analyze every fixture once and share the results across tests."""
    return {f.label: analyze(*load(f)) for f in FIXTURES}


# --------------------------------------------------------------------------
# unit
# --------------------------------------------------------------------------

def test_unescape_reaches_fixed_point():
    # EDGAR headers double-escape ampersands.
    assert unescape_stable("S&amp;amp;P 500") == "S&P 500"


def test_fund_key_normalizes_trademark_and_former_names():
    assert fund_key("VY(R) BrandywineGLOBAL - Bond Portfolio") == fund_key(
        "VY ® BrandywineGLOBAL- Bond Portfolio"
    )
    assert fund_key("Guardian Small Cap Value Diversified VIP Fund "
                    "(formerly, Guardian Small Cap Core VIP Fund)") == fund_key(
        "Guardian Small Cap Value Diversified VIP Fund"
    )


def test_textify_drops_scripts_and_collapses_whitespace():
    assert textify("<p>a</p><script>var x = '<b>';</script><p>b</p>") == "a b"


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.label)
def test_classification(fixture, analyses):
    assert analyses[fixture.label].kind == FilingKind(fixture.kind)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.label)
def test_series_roster(fixture, analyses):
    assert len(analyses[fixture.label].header.series) == fixture.series


def test_closed_end_is_skipped_with_a_reason():
    """Closed-end filings must be recorded, never silently dropped."""
    for label in ("closedend", "closedend2"):
        result = analyze(*load(BY_LABEL[label]))
        assert not result.supported
        assert result.skip_reason
        assert result.manifest()["skip_reason"]


# --------------------------------------------------------------------------
# sectioning
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fixture", [f for f in FIXTURES if f.sections is not None], ids=lambda f: f.label
)
def test_item7_section_count(fixture, analyses):
    assert len(analyses[fixture.label].spans) == fixture.sections


def test_spans_are_ordered_and_disjoint(analyses):
    for label, result in analyses.items():
        spans = result.spans
        for earlier, later in zip(spans, spans[1:]):
            assert earlier.end <= later.start, f"{label}: overlapping spans"


# --------------------------------------------------------------------------
# audit coverage
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fixture", ANNUAL, ids=lambda f: f.label)
def test_opinion_count(fixture, analyses):
    assert analyses[fixture.label].reconciliation.opinions == fixture.opinions


@pytest.mark.parametrize("fixture", ANNUAL, ids=lambda f: f.label)
def test_every_series_is_covered_by_an_opinion(fixture, analyses):
    rec = analyses[fixture.label].reconciliation
    assert rec.uncovered == {}, f"uncovered: {sorted(rec.uncovered.values())}"
    assert len(rec.covered) == fixture.covered


def test_semiannual_has_no_opinion_and_no_coverage_check():
    """N-CSRS is unaudited: running coverage would report a spurious gap."""
    result = analyze(*load(BY_LABEL["semiannual"]))
    assert result.supported
    assert not result.audited
    assert result.reconciliation is None
    assert find_opinions(textify(load(BY_LABEL["semiannual"])[0])) == []


def test_audited_flag_tracks_form_type(analyses):
    assert analyses["penn"].audited is True
    assert analyses["semiannual"].audited is False


# --------------------------------------------------------------------------
# corpus-level invariant
# --------------------------------------------------------------------------

def test_full_corpus_reconciles(analyses):
    total = covered = 0
    for fixture in ANNUAL:
        rec = analyses[fixture.label].reconciliation
        total += rec.total
        covered += len(rec.covered)
    assert (covered, total) == (101, 101)


# --------------------------------------------------------------------------
# attribution (milestone 2)
# --------------------------------------------------------------------------

#: Filings whose attribution is currently below threshold and therefore routed
#: to review. Victory concatenates four complete reports inside one Item 7 span,
#: so its opinion sections run through report boundaries; Guggenheim and the
#: master/feeder pair leave large stretches unattributed. Tracked as a known
#: gap, not silently accepted -- see README "Known limitations".
KNOWN_LOW_ATTRIBUTION = {"gugg", "victory", "blackrock", "feeder"}

#: Per-filing attribution floors. Pinned so a regression shows up as a specific
#: filing getting worse rather than a corpus average drifting quietly.
COVERAGE_FLOOR = {
    "penn": 0.98, "guard": 0.98, "gugg": 0.62, "imst": 0.94,
    "templeton": 0.89, "blackrock": 0.82, "victory": 0.35, "nlfund": 0.94,
    "voya": 1.0, "consolidated": 1.0, "master": 0.87, "feeder": 0.84,
}


@pytest.mark.parametrize("fixture", ANNUAL, ids=lambda f: f.label)
def test_every_series_has_a_holdings_schedule(fixture, analyses):
    """A fund with no schedule of investments is either a parsing miss or a
    feeder holding only master shares. Only `feeder` is a known genuine case."""
    result = analyses[fixture.label]
    missing = set(result.header.series) - result.series_with_schedule
    if fixture.label == "feeder":
        # BlackRock Cash Funds: Treasury invests through a master portfolio.
        assert len(missing) <= 1
    else:
        assert not missing, f"no schedule for {sorted(missing)}"


@pytest.mark.parametrize("fixture", ANNUAL, ids=lambda f: f.label)
def test_attribution_meets_threshold_or_is_flagged(fixture, analyses):
    """Every filing either attributes well or is routed to review. There is no
    third state where weak attribution flows downstream unnoticed."""
    result = analyses[fixture.label]
    assert result.attribution_coverage >= COVERAGE_FLOOR[fixture.label]
    if fixture.label in KNOWN_LOW_ATTRIBUTION:
        assert result.needs_review
    else:
        assert result.attribution_coverage >= 0.85
        assert not result.needs_review


def test_sections_tile_their_span(analyses):
    """Sections must partition each span with no gaps or overlaps, so every
    character of Item 7 is accounted for."""
    for label, result in analyses.items():
        if not result.spans:
            continue
        for span in result.spans:
            inside = [s for s in result.sections if span.start <= s.start < span.end]
            for earlier, later in zip(inside, inside[1:]):
                assert earlier.end == later.start, f"{label}: non-contiguous"
            if inside:
                assert inside[-1].end <= span.end, f"{label}: overruns span"


def test_shared_sections_carry_every_named_fund(analyses):
    """Penn's Statement of Operations is columnar, four funds at a time."""
    shared = [s for s in analyses["penn"].sections if len(s.series_ids) > 1]
    assert shared, "expected multi-fund columnar statements"
    assert max(len(s.series_ids) for s in shared) >= 3


def test_corpus_attribution_coverage(analyses):
    """Corpus-level regression guard on overall attribution quality."""
    specific = sum(analyses[f.label].fund_specific_chars for f in ANNUAL)
    attributed = sum(analyses[f.label].attributed_chars for f in ANNUAL)
    assert attributed / specific >= 0.90


# --------------------------------------------------------------------------
# master-feeder (look-through policy)
# --------------------------------------------------------------------------

def test_feeder_series_resolve_to_their_masters(analyses):
    """Every series in BlackRock Funds III is a feeder, and each names the
    master portfolio it invests through."""
    structure = analyses["feeder"].master_feeder
    assert len(structure.feeders) == 7
    assert not structure.masters
    assert all(m.endswith("Master Portfolio") for m in structure.feeders.values())


def test_master_series_are_excluded_from_default_aggregates(analyses):
    """Look-through credits the feeder, so the master's own rows would double
    count and must be excluded by default."""
    structure = analyses["master"].master_feeder
    assert len(structure.masters) == 8
    assert not structure.feeders
    assert structure.excluded_from_aggregates() == sorted(structure.masters)


def test_a_series_is_never_both_master_and_feeder(analyses):
    for result in analyses.values():
        structure = result.master_feeder
        assert not (set(structure.feeders) & set(structure.masters))


def test_no_master_feeder_false_positives(analyses):
    """Only the master/feeder pair has this structure; the rest must be clean."""
    for label, result in analyses.items():
        if label in {"master", "feeder"}:
            continue
        assert not result.master_feeder.has_structure, label


def test_feeder_without_own_schedule_is_not_flagged(analyses):
    """BlackRock Cash Funds: Treasury holds only master shares, so having no
    schedule of its own is expected -- it must not count as a parsing miss."""
    result = analyses["feeder"]
    missing = set(result.header.series) - result.series_with_schedule
    assert missing
    assert missing <= set(result.master_feeder.feeders)
