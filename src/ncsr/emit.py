"""Turn a ``FilingAnalysis`` into archived evidence, table rows, and a manifest.

Write order is load-bearing -- see ``store``. Evidence and rows first, manifest
last, because the manifest is what marks the filing complete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .attribution import TRUSTWIDE_SECTIONS
from .pipeline import FilingAnalysis
from .records import FindingRecord, Provenance, SectionRecord
from .store import Store

#: Path segments used when a section belongs to no single fund.
SHARED = "_shared"
TRUST = "_trust"


def _series_segment(series_ids) -> str:
    if len(series_ids) == 1:
        return series_ids[0]
    return SHARED if series_ids else TRUST


def archive_key(analysis: FilingAnalysis, section, span_index: int) -> str:
    """Archival object key.

    Fund appears in the path so the tree is browsable per fund, as intended.
    Content is written once -- a columnar statement covering four funds lands
    under ``_shared`` rather than being duplicated under each -- and the section
    row carries the full ``series_ids`` list so a UI can still group by fund.
    """
    header = analysis.header
    return "/".join(
        [
            "filings",
            f"cik={header.cik}",
            f"accession={header.accession}",
            f"series={_series_segment(section.series_ids)}",
            f"section={section.section_type}",
            f"{span_index:03d}-{section.start:08d}.txt",
        ]
    )


@dataclass
class EmitResult:
    objects: int = 0
    sections: int = 0
    findings: int = 0
    manifest: Dict[str, object] = field(default_factory=dict)
    skipped: bool = False


def _provenance(
    analysis: FilingAnalysis,
    series_id: Optional[str] = None,
    section_type: Optional[str] = None,
    char_start: Optional[int] = None,
    char_end: Optional[int] = None,
    method: str = "table_parser",
    confidence: float = 1.0,
) -> Provenance:
    header = analysis.header
    return Provenance(
        accession=header.accession or "",
        cik=header.cik or "",
        period=header.period or "",
        pipeline_version=analysis.pipeline_version,
        audited=analysis.audited,
        series_id=series_id,
        section_type=section_type,
        char_start=char_start,
        char_end=char_end,
        method=method,
        confidence=confidence,
    )


def build_findings(analysis: FilingAnalysis) -> List[FindingRecord]:
    """Derive review findings from the deterministic checks.

    These are rule-derived (``method="regex"``, confidence 1.0). The LLM stages
    will append to the same table with ``method="llm"`` and a real confidence,
    so a reviewer can filter by how a finding was reached.
    """
    findings: List[FindingRecord] = []
    reconciliation = analysis.reconciliation

    if reconciliation is not None:
        for series_id, name in sorted(reconciliation.uncovered.items()):
            findings.append(
                FindingRecord(
                    provenance=_provenance(analysis, series_id, method="regex"),
                    finding_type="audit_opinion_coverage",
                    severity="exception",
                    summary=(
                        f"{name} appears in the filing's series roster but is not "
                        f"named in any audit opinion."
                    ),
                    passed=False,
                )
            )
        if reconciliation.is_complete:
            findings.append(
                FindingRecord(
                    provenance=_provenance(analysis, method="regex"),
                    finding_type="audit_opinion_coverage",
                    severity="info",
                    summary=(
                        f"All {len(reconciliation.covered)} series are named across "
                        f"{reconciliation.opinions} audit opinion(s)."
                    ),
                    passed=True,
                )
            )

    # A feeder holding only master shares legitimately has no schedule.
    expected = set(analysis.header.series) - set(analysis.master_feeder.feeders)
    for series_id in sorted(expected - analysis.series_with_schedule):
        findings.append(
            FindingRecord(
                provenance=_provenance(analysis, series_id, method="regex"),
                finding_type="missing_holdings_schedule",
                severity="warning",
                summary=(
                    f"No schedule of investments located for "
                    f"{analysis.header.series[series_id]}."
                ),
                passed=False,
            )
        )

    for series_id, master in sorted(analysis.master_feeder.feeders.items()):
        findings.append(
            FindingRecord(
                provenance=_provenance(analysis, series_id, method="regex"),
                finding_type="master_feeder",
                severity="info",
                summary=(
                    f"{analysis.header.series[series_id]} invests through {master}; "
                    f"look-through credits this feeder with the holdings."
                ),
                passed=True,
            )
        )

    for series_id in analysis.master_feeder.masters:
        findings.append(
            FindingRecord(
                provenance=_provenance(analysis, series_id, method="regex"),
                finding_type="master_portfolio_excluded",
                severity="info",
                summary=(
                    f"{analysis.header.series[series_id]} is a master portfolio; "
                    f"excluded from default aggregates to avoid double counting."
                ),
                passed=True,
            )
        )

    if analysis.needs_review:
        findings.append(
            FindingRecord(
                provenance=_provenance(analysis, method="regex"),
                finding_type="attribution_quality",
                severity="warning",
                summary=(
                    f"Only {analysis.attribution_coverage:.1%} of fund-specific "
                    f"content could be attributed to a named fund; queued for review."
                ),
                passed=False,
            )
        )

    return findings


def emit(
    analysis: FilingAnalysis,
    text: str,
    store: Store,
    force: bool = False,
) -> EmitResult:
    """Persist a filing. Returns counts; a no-op when already processed."""
    accession = analysis.header.accession or ""

    if not force and store.is_processed(accession, analysis.pipeline_version):
        return EmitResult(manifest=analysis.manifest(), skipped=True)

    result = EmitResult(manifest=analysis.manifest())

    if analysis.supported:
        span_bounds = [(s.start, s.end) for s in analysis.spans]
        excluded = set(analysis.master_feeder.masters)

        section_rows = []
        for section in analysis.sections:
            span_index = next(
                (i for i, (a, b) in enumerate(span_bounds) if a <= section.start < b),
                0,
            )
            store.put_object(
                archive_key(analysis, section, span_index),
                text[section.start : section.end],
            )
            result.objects += 1

            eligible = not (set(section.series_ids) & excluded)
            section_rows.append(
                SectionRecord(
                    provenance=_provenance(
                        analysis,
                        series_id=section.series_ids[0] if len(section.series_ids) == 1 else None,
                        section_type=section.section_type,
                        char_start=section.start,
                        char_end=section.end,
                    ),
                    span_index=span_index,
                    series_ids=section.series_ids,
                    aggregate_eligible=eligible,
                ).to_row()
            )
        result.sections = store.append_rows("sections", section_rows)

    findings = build_findings(analysis)
    result.findings = store.append_rows("findings", [f.to_row() for f in findings])

    # Last: the commit marker. Everything above is idempotent and supersedable;
    # only this says the filing is done.
    store.put_manifest(result.manifest)
    return result
