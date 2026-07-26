"""Filing classification and the top-level analysis entry point.

Produces the record that becomes the DynamoDB manifest -- the commit marker
written last, which makes reprocessing idempotent and lets a PIPELINE_VERSION
bump invalidate prior runs without a delete step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from .attribution import FundSection, attribute
from .audit import Reconciliation, find_opinions, reconcile_coverage
from .header import Header, parse_header
from .master_feeder import MasterFeeder, detect as detect_master_feeder
from .normalize import textify
from .sectioner import Span, find_item7_spans, split_reports

#: Bump to invalidate every stored manifest and force a backfill.
PIPELINE_VERSION = 1

#: Below this share of fund-specific content attributed to a named fund, the
#: filing is queued for human review rather than trusted downstream.
REVIEW_COVERAGE_THRESHOLD = 0.85


class FilingKind(str, Enum):
    OPEN_END_ANNUAL = "open_end_annual"
    OPEN_END_SEMIANNUAL = "open_end_semiannual"
    #: Registrant is itself the fund: no series roster, financials live in
    #: Item 1 rather than Item 7. Out of v1 scope, recorded not dropped.
    CLOSED_END = "closed_end"
    UNKNOWN = "unknown"


#: Kinds this pipeline version can extract facts from.
SUPPORTED = frozenset({FilingKind.OPEN_END_ANNUAL, FilingKind.OPEN_END_SEMIANNUAL})


def classify(header: Header) -> FilingKind:
    """Route a filing by its header alone -- no document parsing required.

    Absence of a series roster is the closed-end signal, and it is decisive:
    both closed-end samples carried zero SERIES-ID tags, and their Item 6 points
    at Item 1 ("Schedule of Investments is included as part of the Portfolio of
    Investments in Item 1 of this Form N-CSR") rather than Item 7.
    """
    if not header.has_series:
        return FilingKind.CLOSED_END if header.form_type else FilingKind.UNKNOWN
    if header.is_audited:
        return FilingKind.OPEN_END_ANNUAL
    if header.is_semiannual:
        return FilingKind.OPEN_END_SEMIANNUAL
    return FilingKind.UNKNOWN


@dataclass
class FilingAnalysis:
    """Everything the sectioning stage learns about one filing."""

    header: Header
    kind: FilingKind
    spans: List[Span] = field(default_factory=list)
    reconciliation: Optional[Reconciliation] = None
    text_length: int = 0
    pipeline_version: int = PIPELINE_VERSION
    skip_reason: Optional[str] = None
    sections: List[FundSection] = field(default_factory=list)
    fund_specific_chars: int = 0
    attributed_chars: int = 0
    #: series_id -> section types located for that fund
    by_series: Dict[str, set] = field(default_factory=dict)
    master_feeder: MasterFeeder = field(default_factory=MasterFeeder)

    @property
    def supported(self) -> bool:
        return self.kind in SUPPORTED

    @property
    def attribution_coverage(self) -> float:
        """Share of fund-specific content tied to a named fund."""
        if not self.fund_specific_chars:
            return 0.0
        return self.attributed_chars / self.fund_specific_chars

    @property
    def series_with_schedule(self) -> set:
        return {
            sid
            for sid, types in self.by_series.items()
            if "schedule_of_investments" in types
        }

    @property
    def needs_review(self) -> bool:
        """True when attribution is too weak to trust without a human look.

        Either signal alone is enough: a fund with no holdings schedule may be a
        genuine absence (a feeder holding only master shares) or a parsing miss,
        and only a reviewer can tell.
        """
        if not self.supported:
            return False
        if self.attribution_coverage < REVIEW_COVERAGE_THRESHOLD:
            return True
        expected = set(self.header.series) - set(self.master_feeder.feeders)
        return bool(expected - self.series_with_schedule)

    @property
    def audited(self) -> bool:
        """Stamped onto every fact row so audited and unaudited figures are
        never silently compared."""
        return self.header.is_audited

    def manifest(self) -> Dict[str, object]:
        """The DynamoDB commit-marker payload."""
        rec = self.reconciliation
        return {
            "accession": self.header.accession,
            "cik": self.header.cik,
            "form_type": self.header.form_type,
            "period": self.header.period,
            "kind": self.kind.value,
            "audited": self.audited,
            "pipeline_version": self.pipeline_version,
            "series_count": len(self.header.series),
            "item7_sections": len(self.spans),
            "item7_chars": sum(s.length for s in self.spans),
            "opinions": rec.opinions if rec else 0,
            "series_covered": len(rec.covered) if rec else 0,
            "series_uncovered": sorted(rec.uncovered.values()) if rec else [],
            "fund_sections": len(self.sections),
            "attribution_coverage": round(self.attribution_coverage, 4),
            "series_with_schedule": len(self.series_with_schedule),
            "feeder_series": len(self.master_feeder.feeders),
            "master_series": len(self.master_feeder.masters),
            "aggregate_excluded_series": self.master_feeder.excluded_from_aggregates(),
            "needs_review": self.needs_review,
            "skip_reason": self.skip_reason,
        }


def analyze(markup: str, header_markup: str) -> FilingAnalysis:
    """Classify a filing and locate its Item 7 sections and audit opinions."""
    header = parse_header(header_markup)
    kind = classify(header)

    if kind not in SUPPORTED:
        reason = (
            "closed-end registrant: no series roster; financials sit in Item 1"
            if kind is FilingKind.CLOSED_END
            else "unrecognized form type or missing header fields"
        )
        return FilingAnalysis(header=header, kind=kind, skip_reason=reason)

    text = textify(markup)
    spans = find_item7_spans(text)

    # Semi-annual reports are unaudited and carry no opinion, so coverage is
    # not applicable -- running it would report a spurious 0/N gap.
    opinions = find_opinions(text)
    reconciliation = None
    if kind is FilingKind.OPEN_END_ANNUAL:
        reconciliation = reconcile_coverage(header.series, opinions)

    # A span holding several complete reports is split before attribution so an
    # opinion cannot absorb the next report's front matter.
    opinion_offsets = [o.start for o in opinions]
    spans = [part for span in spans for part in split_reports(text, span, opinion_offsets)]

    structure = detect_master_feeder(text, header.series)

    sections: List[FundSection] = []
    fund_specific = attributed = 0
    by_series: Dict[str, set] = {}
    for span in spans:
        result = attribute(text, header.series, span.start, span.end)
        sections.extend(result.sections)
        fund_specific += result.fund_specific_chars
        attributed += result.attributed_chars
        for sid, types in result.by_series.items():
            by_series.setdefault(sid, set()).update(types)

    return FilingAnalysis(
        header=header,
        kind=kind,
        spans=spans,
        reconciliation=reconciliation,
        text_length=len(text),
        sections=sections,
        fund_specific_chars=fund_specific,
        attributed_chars=attributed,
        by_series=by_series,
        master_feeder=structure,
    )
