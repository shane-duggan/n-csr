"""Athena/Iceberg table definitions.

Iceberg rather than raw Parquet because reprocessing is a certainty, not a
contingency: the sectioner alone needed five fixes across the first nine
filings, and each fix means backfilling prior periods. Iceberg gives row-level
``DELETE``/``MERGE`` and snapshot isolation, so superseding one accession is a
query rather than a partition rewrite with a window of partial data.

These tables are *separate* from the archival S3 tree. Writing analytics-ready
Parquet into ``cik=…/accession=…/series=…/section=…/`` would produce on the
order of 700k tiny files a year, which makes Athena both slow and expensive.
The tree holds evidence; these tables answer questions.
"""

from __future__ import annotations

from typing import Dict, Sequence, Tuple

DATABASE = "ncsr"

#: Columns shared by every table, emitted by ``records.Provenance``.
_PROVENANCE: Sequence[Tuple[str, str]] = (
    ("accession", "string"),
    ("cik", "string"),
    ("period", "string"),
    ("fiscal_period", "string"),
    ("pipeline_version", "int"),
    ("audited", "boolean"),
    ("series_id", "string"),
    ("section_type", "string"),
    ("char_start", "int"),
    ("char_end", "int"),
    ("method", "string"),
    ("confidence", "double"),
    ("model_id", "string"),
)

_TABLES: Dict[str, Sequence[Tuple[str, str]]] = {
    "sections": (
        ("span_index", "int"),
        ("series_ids", "array<string>"),
        ("is_shared", "boolean"),
        ("aggregate_eligible", "boolean"),
        ("char_length", "int"),
    ),
    "findings": (
        ("finding_type", "string"),
        ("severity", "string"),
        ("summary", "string"),
        ("excerpt", "string"),
        ("passed", "boolean"),
    ),
    "holdings": (
        ("issuer", "string"),
        ("identifier", "string"),
        ("identifier_kind", "string"),
        ("coupon", "string"),
        ("maturity_date", "string"),
        ("shares_or_par", "string"),
        ("value", "string"),
        ("cost", "string"),
        ("fair_value_level", "int"),
        ("flags", "array<string>"),
        ("category", "string"),
        ("aggregate_eligible", "boolean"),
    ),
    "fair_value_levels": (
        ("category", "string"),
        ("level", "int"),
        ("amount", "string"),
        ("is_total_row", "boolean"),
        ("aggregate_eligible", "boolean"),
    ),
    "statement_lines": (
        ("caption", "string"),
        ("value", "string"),
        ("column_index", "int"),
        ("aggregate_eligible", "boolean"),
    ),
}

#: Every table partitions on fiscal period. Queries are nearly always scoped to
#: a reporting period, and it keeps partition counts in the hundreds.
PARTITION_BY = "fiscal_period"


def columns(table: str) -> Sequence[Tuple[str, str]]:
    """Full column list for a table: provenance first, then payload."""
    if table not in _TABLES:
        raise KeyError(f"unknown table: {table}")
    return tuple(_PROVENANCE) + tuple(_TABLES[table])


def create_table(table: str, warehouse: str, database: str = DATABASE) -> str:
    """Render the Athena ``CREATE TABLE`` statement for an Iceberg table."""
    body = ",\n".join(f"  {name} {sql_type}" for name, sql_type in columns(table))
    location = f"{warehouse.rstrip('/')}/tables/{table}/"
    return (
        f"CREATE TABLE IF NOT EXISTS {database}.{table} (\n{body}\n)\n"
        f"PARTITIONED BY ({PARTITION_BY})\n"
        f"LOCATION '{location}'\n"
        f"TBLPROPERTIES (\n"
        f"  'table_type' = 'ICEBERG',\n"
        f"  'format' = 'parquet',\n"
        f"  'write_compression' = 'zstd'\n"
        f");"
    )


def create_all(warehouse: str, database: str = DATABASE) -> str:
    statements = [f"CREATE DATABASE IF NOT EXISTS {database};"]
    statements += [create_table(name, warehouse, database) for name in _TABLES]
    return "\n\n".join(statements)


def supersede(table: str, accession: str, database: str = DATABASE) -> str:
    """Delete a filing's rows so a reprocess can replace them atomically.

    This is the operation raw Parquet cannot do cleanly, and the reason the
    tables are Iceberg.
    """
    return (
        f"DELETE FROM {database}.{table} WHERE accession = '{accession}';"
    )


#: Reference query for "which funds hold the most Level 3 securities".
#:
#: Sourced from the fund's own fair-value hierarchy disclosure rather than from
#: per-security footnote symbols, which are filing-local and ambiguous. The
#: total row is excluded so it is not summed with the asset-class rows it
#: totals.
LEVEL_3_BY_FUND = """
SELECT series_id,
       sum(try_cast(amount AS double)) AS level_3_value
FROM {database}.fair_value_levels
WHERE fiscal_period = ?
  AND level = 3
  AND NOT is_total_row            -- do not double count the total row
  AND aggregate_eligible          -- look-through: excludes master portfolios
  AND audited                     -- never mix audited and unaudited figures
GROUP BY series_id
HAVING sum(try_cast(amount AS double)) > 0
ORDER BY level_3_value DESC
"""
