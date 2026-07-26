"""Tests for candidate retrieval, the model seam, and the review stages.

No network and no credentials: ``ScriptedJudge`` stands in for the model, so
retrieval, prompting, verdict parsing and finding construction are all covered.
"""

from __future__ import annotations

import pytest

from ncsr.audit import find_opinions
from ncsr.candidates import EXCLUSIONS, retrieve, looks_like_boilerplate
from ncsr.llm import ScriptedJudge, Verdict, parse_verdicts
from ncsr.normalize import textify
from ncsr.pipeline import analyze
from ncsr.review import (
    CONTINGENCY_SYSTEM,
    VERDICT_SCHEMA,
    build_prompt,
    triage_contingencies,
    unreviewed_passages,
)

from fixtures import FIXTURES, BY_LABEL, load

SUPPORTED = [f for f in FIXTURES if f.kind != "closed_end"]


@pytest.fixture(scope="module")
def victory():
    document, header = load(BY_LABEL["victory"])
    analysis = analyze(document, header)
    text = textify(document)
    return analysis, text, retrieve(text, analysis.sections)


# --------------------------------------------------------------------------
# retrieval
# --------------------------------------------------------------------------

def test_retrieval_stays_small_enough_to_be_cheap():
    """The whole point of retrieving rather than sending the notes wholesale."""
    total = 0
    for fixture in SUPPORTED:
        document, header = load(fixture)
        analysis = analyze(document, header)
        if not analysis.supported:
            continue
        total += sum(p.length for p in retrieve(textify(document), analysis.sections))
    assert total < 60_000, f"{total:,} characters is too much to send per corpus"


def test_trade_settlement_is_not_retrieved_as_a_legal_settlement():
    """"valued at the last settlement price" is trade settlement. A bare
    "settlement" trigger produced 213 hits on one filing, nearly all pricing."""
    document, header = load(BY_LABEL["guard"])
    analysis = analyze(document, header)
    passages = retrieve(textify(document), analysis.sections)
    assert len(passages) < 10
    assert not any("settlement price" in p.text for p in passages)


def test_expense_cap_language_is_excluded():
    """"excluding ... litigation and extraordinary expenses" is a fee
    definition, and accounted for 16 of Guardian's 19 retrieved passages."""
    assert EXCLUSIONS.search("excluding litigation and extraordinary expenses)")


def test_passage_carries_the_sentence_that_decides_it(victory):
    """An indemnification note only reads as boilerplate once you reach "the
    risk of loss to be remote"; cutting at the first full stop hides it."""
    document, header = load(BY_LABEL["penn"])
    analysis = analyze(document, header)
    passages = retrieve(textify(document), analysis.sections)
    assert passages
    assert looks_like_boilerplate(passages[0])
    assert "risk of loss to be remote" in passages[0].text


def test_passages_are_deduplicated():
    """A filing repeating one note per fund must not be judged once per copy."""
    document, header = load(BY_LABEL["guard"])
    analysis = analyze(document, header)
    passages = retrieve(textify(document), analysis.sections)
    openings = [p.text[:120] for p in passages]
    assert len(openings) == len(set(openings))


# --------------------------------------------------------------------------
# the model seam
# --------------------------------------------------------------------------

def test_verdicts_for_unsent_passages_are_dropped():
    """Reindexing would attach a rationale to the wrong excerpt."""
    raw = [
        {"index": 0, "verdict": "boilerplate", "confidence": 0.9, "rationale": "x"},
        {"index": 9, "verdict": "substantive", "confidence": 0.9, "rationale": "y"},
    ]
    assert [v.index for v in parse_verdicts(raw, expected=2)] == [0]


def test_malformed_verdicts_are_discarded():
    raw = [
        {"index": "nope", "verdict": "boilerplate", "confidence": 1, "rationale": ""},
        {"index": 0, "verdict": "made_up", "confidence": 1, "rationale": ""},
        {"index": 0, "confidence": 1, "rationale": ""},
    ]
    assert parse_verdicts(raw, expected=2) == []


def test_confidence_is_clamped():
    raw = [{"index": 0, "verdict": "substantive", "confidence": 5, "rationale": "r"}]
    assert parse_verdicts(raw, expected=1)[0].confidence == 1.0


# --------------------------------------------------------------------------
# contingency triage
# --------------------------------------------------------------------------

def test_prompt_numbers_every_passage(victory):
    _, _, passages = victory
    prompt = build_prompt(passages)
    for index in range(len(passages)):
        assert f'index="{index}"' in prompt


def test_findings_are_marked_as_model_derived(victory):
    analysis, text, passages = victory
    judge = ScriptedJudge(
        [
            {"index": i, "verdict": "boilerplate", "confidence": 0.9, "rationale": "r"}
            for i in range(len(passages))
        ]
    )
    findings = triage_contingencies(analysis, text, judge, passages)
    assert findings
    for finding in findings:
        row = finding.to_row()
        assert row["method"] == "llm"
        assert row["model_id"] == "scripted"
        assert row["excerpt"]


def test_substantive_verdict_becomes_an_exception(victory):
    analysis, text, passages = victory
    judge = ScriptedJudge(
        [{"index": 0, "verdict": "substantive", "confidence": 0.8, "rationale": "accrued $1.2m"}]
    )
    findings = triage_contingencies(analysis, text, judge, passages)
    assert len(findings) == 1
    assert findings[0].severity == "exception"
    assert findings[0].passed is False


def test_boilerplate_is_recorded_not_discarded(victory):
    """A reviewer needs to see the language was found and judged, not missed."""
    analysis, text, passages = victory
    judge = ScriptedJudge(
        [{"index": 0, "verdict": "boilerplate", "confidence": 0.95, "rationale": "remote"}]
    )
    findings = triage_contingencies(analysis, text, judge, passages)
    assert findings[0].severity == "info"
    assert findings[0].passed is True


def test_offsets_round_trip_to_the_filing(victory):
    analysis, text, passages = victory
    judge = ScriptedJudge(
        [{"index": i, "verdict": "boilerplate", "confidence": 0.9, "rationale": "r"}
         for i in range(len(passages))]
    )
    for finding in triage_contingencies(analysis, text, judge, passages):
        start = finding.provenance.char_start
        end = finding.provenance.char_end
        assert text[start:end].strip() == finding.excerpt


def test_unjudged_passages_are_surfaced(victory):
    """A dropped verdict must not look like a clean result."""
    analysis, text, passages = victory
    judge = ScriptedJudge(
        [{"index": 0, "verdict": "boilerplate", "confidence": 0.9, "rationale": "r"}]
    )
    findings = triage_contingencies(analysis, text, judge, passages)
    assert len(unreviewed_passages(passages, findings)) == len(passages) - 1


def test_schema_is_closed():
    """A closed schema keeps the model from inventing fields."""
    assert VERDICT_SCHEMA["additionalProperties"] is False
    item = VERDICT_SCHEMA["properties"]["verdicts"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == {"index", "verdict", "confidence", "rationale"}


def test_rubric_names_the_boilerplate_pattern():
    assert "risk of loss" in CONTINGENCY_SYSTEM
    assert "trade settlement" in CONTINGENCY_SYSTEM


# --------------------------------------------------------------------------
# audit-opinion elements (deterministic)
# --------------------------------------------------------------------------

def test_required_opinion_elements_are_extracted():
    document, header = load(BY_LABEL["penn"])
    opinion = find_opinions(textify(document))[0]
    assert opinion.auditor == "KPMG LLP"
    assert opinion.auditor_since == "2004"
    assert opinion.report_date == "February 24, 2026"
    assert opinion.cites_pcaob
    assert opinion.missing_elements() == []


def test_an_officer_certification_is_not_read_as_an_auditor_signature():
    """Guggenheim's "/s/" is the CEO certifying the report, not the auditor
    signing the opinion; the match requires a firm suffix."""
    document, header = load(BY_LABEL["gugg"])
    opinion = find_opinions(textify(document))[0]
    assert "/s/ Brian Binder" in opinion._elements
    assert opinion.auditor is None
    assert "auditor signature" in opinion.missing_elements()


def test_fund_names_containing_periods_do_not_break_tenure():
    """"...one or more Penn Series Funds, Inc. investment companies since 2004"."""
    document, header = load(BY_LABEL["penn"])
    assert find_opinions(textify(document))[0].auditor_since == "2004"


def test_opinion_window_is_bounded_by_the_next_opinion():
    """A fixed 6,000-character window was too tight for a filing that
    enumerates 25 funds -- nine read as uncovered when the opinion named them.
    Bounding by the next opinion instead keeps coverage from bleeding across
    concatenated reports, which is what the tight limit was guarding against."""
    document, header = load(BY_LABEL["guard"])
    analysis = analyze(document, header)
    opinions = find_opinions(textify(document))
    assert len(opinions) == 24
    for earlier, later in zip(opinions, opinions[1:]):
        assert earlier.start + len(earlier.text) <= later.start
    assert analysis.reconciliation.uncovered == {}
