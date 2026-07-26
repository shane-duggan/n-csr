"""Tests for the storage layer: schemas, emit, and commit semantics."""

from __future__ import annotations

import json
import os

import pytest

from ncsr import ddl
from ncsr.emit import build_findings, emit
from ncsr.normalize import textify
from ncsr.pipeline import analyze
from ncsr.records import (
    FairValueRecord,
    FindingRecord,
    HoldingRecord,
    Provenance,
    SectionRecord,
    StatementLineRecord,
)
from ncsr.store import LocalStore

from fixtures import BY_LABEL, load

PROVENANCE = Provenance(
    accession="0001193125-26-092803",
    cik="0000702340",
    period="20251231",
    pipeline_version=1,
    audited=True,
)


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "table,row",
    [
        ("sections", SectionRecord(PROVENANCE, 0, ("S1",)).to_row()),
        ("findings", FindingRecord(PROVENANCE, "t", "info", "s").to_row()),
        ("holdings", HoldingRecord(PROVENANCE, "Acme Corp").to_row()),
        ("statement_lines", StatementLineRecord(PROVENANCE, "Dividends").to_row()),
        ("fair_value_levels", FairValueRecord(PROVENANCE, "Corporate Bonds", 3, "0").to_row()),
    ],
)
def test_ddl_matches_row_shape(table, row):
    """Schema and rows must not drift: a column with no field silently reads
    null, and a field with no column is silently dropped on write."""
    columns = {name for name, _ in ddl.columns(table)}
    assert set(row) == columns


def test_reserved_words_are_not_used_as_columns():
    """`end`, `start`, `order` and friends are reserved in Trino/Athena."""
    reserved = {"end", "start", "order", "group", "table", "select", "from"}
    for table in TABLES:
        assert not {n for n, _ in ddl.columns(table)} & reserved


def test_fiscal_period_partition_derivation():
    assert PROVENANCE.fiscal_period == "2025-12"
    assert Provenance("a", "c", "", 1, True).fiscal_period == "unknown"


TABLES = ("sections", "findings", "holdings", "statement_lines", "fair_value_levels")


def test_every_table_is_iceberg_and_partitioned():
    sql = ddl.create_all("s3://bucket/ncsr")
    assert sql.count("'table_type' = 'ICEBERG'") == len(TABLES)
    assert sql.count("PARTITIONED BY (fiscal_period)") == len(TABLES)
    for table in TABLES:
        assert f"ncsr.{table} (" in sql


# --------------------------------------------------------------------------
# emit
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def emitted(tmp_path_factory):
    """Emit a representative filing once."""
    root = str(tmp_path_factory.mktemp("store"))
    store = LocalStore(root)
    document, header = load(BY_LABEL["penn"])
    analysis = analyze(document, header)
    result = emit(analysis, textify(document), store)
    return root, store, analysis, result


def test_emit_writes_evidence_rows_and_manifest(emitted):
    root, _, analysis, result = emitted
    assert result.objects == len(analysis.sections)
    assert result.sections == len(analysis.sections)
    assert result.findings > 0
    assert os.path.exists(
        os.path.join(root, "manifests", f"{analysis.header.accession}.json")
    )


def test_archive_path_is_browsable_by_fund(emitted):
    root, _, analysis, _ = emitted
    keys = []
    for dirpath, _, files in os.walk(os.path.join(root, "filings")):
        keys.extend(os.path.join(dirpath, f) for f in files)
    assert keys
    sample = keys[0]
    for segment in ("cik=", "accession=", "series=", "section="):
        assert segment in sample


def test_archived_text_matches_the_recorded_offsets(emitted):
    """Lineage must round-trip: the offsets on a row have to point at the
    text archived for it, or a reviewer cannot verify a number."""
    root, _, analysis, _ = emitted
    document, _ = load(BY_LABEL["penn"])
    text = textify(document)
    partition = os.path.join(root, "tables", "sections", "fiscal_period=2025-12", "rows.jsonl")
    with open(partition, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle]
    assert rows
    for row in rows[:20]:
        assert text[row["char_start"] : row["char_end"]]
        assert row["char_length"] == row["char_end"] - row["char_start"]


def test_emit_is_idempotent(emitted):
    root, store, analysis, _ = emitted
    document, _ = load(BY_LABEL["penn"])
    again = emit(analysis, textify(document), store)
    assert again.skipped
    assert again.objects == 0


def test_pipeline_version_bump_forces_reprocess(emitted):
    """A stored manifest from an older version must not mark a filing done."""
    _, store, analysis, _ = emitted
    accession = analysis.header.accession
    assert store.is_processed(accession, analysis.pipeline_version)
    assert not store.is_processed(accession, analysis.pipeline_version + 1)


def test_manifest_is_written_last(tmp_path):
    """A crash before the manifest must leave the filing re-runnable."""

    class FailingStore(LocalStore):
        def put_manifest(self, manifest):
            raise RuntimeError("crash before commit")

    store = FailingStore(str(tmp_path))
    document, header = load(BY_LABEL["templeton"])
    analysis = analyze(document, header)
    with pytest.raises(RuntimeError):
        emit(analysis, textify(document), store)
    # Rows were written, but the filing is not marked complete.
    assert not store.is_processed(analysis.header.accession, analysis.pipeline_version)


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------

def test_complete_coverage_produces_a_passing_finding():
    document, header = load(BY_LABEL["penn"])
    findings = build_findings(analyze(document, header))
    coverage = [f for f in findings if f.finding_type == "audit_opinion_coverage"]
    assert coverage and all(f.passed for f in coverage)


def test_master_portfolios_are_recorded_as_excluded():
    document, header = load(BY_LABEL["master"])
    findings = build_findings(analyze(document, header))
    excluded = [f for f in findings if f.finding_type == "master_portfolio_excluded"]
    assert len(excluded) == 8


def test_feeder_holdings_are_marked_aggregate_eligible(tmp_path):
    """Look-through: the feeder keeps its rows, the master's are excluded."""
    store = LocalStore(str(tmp_path))
    for label in ("feeder", "master"):
        document, header = load(BY_LABEL[label])
        emit(analyze(document, header), textify(document), store)

    rows = []
    for dirpath, _, files in os.walk(os.path.join(str(tmp_path), "tables", "sections")):
        for name in files:
            with open(os.path.join(dirpath, name), encoding="utf-8") as handle:
                rows.extend(json.loads(line) for line in handle)

    master_rows = [r for r in rows if r["cik"] == "0000915092" and r["series_id"]]
    feeder_rows = [r for r in rows if r["cik"] == "0000893818" and r["series_id"]]
    assert master_rows and feeder_rows
    assert not any(r["aggregate_eligible"] for r in master_rows)
    assert all(r["aggregate_eligible"] for r in feeder_rows)


def test_low_attribution_filings_emit_a_review_finding():
    document, header = load(BY_LABEL["victory"])
    findings = build_findings(analyze(document, header))
    assert any(f.finding_type == "attribution_quality" for f in findings)
