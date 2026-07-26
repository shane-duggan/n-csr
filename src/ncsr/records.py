"""Row shapes for the analytical tables.

These dataclasses are the contract between extraction and storage. Every row
carries enough lineage to answer "where did this number come from?" without
re-parsing the filing: the accession, the fund, the section, and the character
range in the normalized text archived alongside it.

Three invariants apply to every table:

``audited``
    Sourced from the form type. Audited and unaudited figures must never be
    compared silently, so the flag travels with the row rather than being
    inferred at query time.

``aggregate_eligible``
    False for master-portfolio series. Under the look-through policy the feeder
    carries the position, so counting the master's own rows as well would double
    count. Default aggregates filter on this; opting in is explicit.

``pipeline_version``
    Lets a reprocess supersede prior rows for the same accession without a
    delete step.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class Provenance:
    """Where a row came from. Embedded in every table."""

    accession: str
    cik: str
    period: str
    pipeline_version: int
    audited: bool
    #: None for trust-wide content such as notes.
    series_id: Optional[str] = None
    section_type: Optional[str] = None
    #: Character range in the archived normalized text. Named char_* because
    #: `end` (and `start`) are reserved words in Trino/Athena SQL.
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    #: How the value was obtained: table_parser | regex | legend_map | llm.
    method: str = "table_parser"
    confidence: float = 1.0
    #: Populated only when method == "llm".
    model_id: Optional[str] = None

    @property
    def fiscal_period(self) -> str:
        """Partition key for the analytical tables (YYYY-MM)."""
        if self.period and len(self.period) >= 6:
            return f"{self.period[:4]}-{self.period[4:6]}"
        return "unknown"


def _flatten(provenance: Provenance, payload: Dict[str, Any]) -> Dict[str, Any]:
    row = asdict(provenance)
    row["fiscal_period"] = provenance.fiscal_period
    row.update(payload)
    return row


@dataclass(frozen=True)
class SectionRecord:
    """One attributed stretch of Item 7. The archival index."""

    provenance: Provenance
    span_index: int
    #: Every fund named by a columnar statement, not just the first.
    series_ids: Tuple[str, ...] = ()
    aggregate_eligible: bool = True

    def to_row(self) -> Dict[str, Any]:
        return _flatten(
            self.provenance,
            {
                "span_index": self.span_index,
                "series_ids": list(self.series_ids),
                "is_shared": len(self.series_ids) > 1,
                "aggregate_eligible": self.aggregate_eligible,
                "char_length": (self.provenance.char_end or 0)
                - (self.provenance.char_start or 0),
            },
        )


@dataclass(frozen=True)
class FindingRecord:
    """A review observation: rule-derived or model-derived.

    ``excerpt`` is the verbatim text a reviewer sees, and the provenance offsets
    point at it in the archived section text.
    """

    provenance: Provenance
    finding_type: str
    severity: str  # info | warning | exception
    summary: str
    excerpt: str = ""
    passed: Optional[bool] = None

    def to_row(self) -> Dict[str, Any]:
        return _flatten(
            self.provenance,
            {
                "finding_type": self.finding_type,
                "severity": self.severity,
                "summary": self.summary,
                "excerpt": self.excerpt,
                "passed": self.passed,
            },
        )


@dataclass(frozen=True)
class HoldingRecord:
    """One security in a fund's schedule of investments.

    Populated by the table extractor (roadmap item 5). Defined here so the
    schema, lineage, and look-through flag are settled before rows exist.

    Security identity is the hard part: N-CSR schedules frequently carry no
    CUSIP, so ``issuer``/``coupon``/``maturity_date`` are the composite key used
    to track a position across periods, and ``identifier`` stays null far more
    often than not.
    """

    provenance: Provenance
    issuer: str
    aggregate_eligible: bool = True
    identifier: Optional[str] = None  # CUSIP/ISIN when disclosed
    identifier_kind: Optional[str] = None
    coupon: Optional[str] = None
    maturity_date: Optional[str] = None
    shares_or_par: Optional[str] = None
    value: Optional[str] = None
    cost: Optional[str] = None
    #: Fair-value hierarchy level, 1-3, when disclosed.
    fair_value_level: Optional[int] = None
    #: Resolved from the filing's own footnote legend.
    flags: Tuple[str, ...] = ()
    category: Optional[str] = None

    def to_row(self) -> Dict[str, Any]:
        return _flatten(
            self.provenance,
            {
                "issuer": self.issuer,
                "identifier": self.identifier,
                "identifier_kind": self.identifier_kind,
                "coupon": self.coupon,
                "maturity_date": self.maturity_date,
                "shares_or_par": self.shares_or_par,
                "value": self.value,
                "cost": self.cost,
                "fair_value_level": self.fair_value_level,
                "flags": list(self.flags),
                "category": self.category,
                "aggregate_eligible": self.aggregate_eligible,
            },
        )


@dataclass(frozen=True)
class StatementLineRecord:
    """One line item from a financial statement.

    Statements are frequently columnar, several funds side by side, so the row
    is keyed on (series_id, caption) with the column index retained for audit.
    """

    provenance: Provenance
    caption: str
    aggregate_eligible: bool = True
    value: Optional[str] = None
    column_index: Optional[int] = None

    def to_row(self) -> Dict[str, Any]:
        return _flatten(
            self.provenance,
            {
                "caption": self.caption,
                "value": self.value,
                "column_index": self.column_index,
                "aggregate_eligible": self.aggregate_eligible,
            },
        )
