"""Extract line items from the financial statements.

Statements are usually columnar -- Penn Series reports four funds side by side
in one Statement of Operations -- so the fund is a property of the *column*, not
of the row or even of the section. Column mapping is therefore read from the
table's own header row rather than inherited from section attribution, which is
both more precise and self-checking: a table whose header names no known fund is
skipped rather than guessed at.

Values are normalized to a plain numeric string so ``try_cast(value AS double)``
works in Athena, while the untouched cell text stays reachable through the
row's character offsets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .divtables import tables_for
from .htmltables import ParsedDocument, Table
from .normalize import fund_key, normalize_value

#: Section types whose tables are line-item statements.
STATEMENT_SECTIONS = frozenset(
    {
        "statement_of_assets_and_liabilities",
        "statement_of_operations",
        "statement_of_changes_in_net_assets",
        "statement_of_cash_flows",
    }
)

#: Rows scanned for fund names before giving up on a table.
_HEADER_SCAN_ROWS = 4

@dataclass(frozen=True)
class LineItem:
    """One caption/value pair tied to a fund and a source cell."""

    series_id: str
    caption: str
    value: str
    column_index: int
    char_start: int
    char_end: int


def _column_map(
    table: Table, document: str, series: Dict[str, str]
) -> Tuple[Dict[int, str], int]:
    """Map grid column index -> series_id using the table's header row.

    Returns the mapping and the index of the header row, so callers can skip
    everything at or above it.
    """
    keyed = {fund_key(name): sid for sid, name in series.items() if fund_key(name)}
    grid = table.grid(document)

    best: Dict[int, str] = {}
    best_row = -1
    for row_index, row in enumerate(grid[:_HEADER_SCAN_ROWS]):
        mapping: Dict[int, str] = {}
        for column, cell in enumerate(row):
            key = fund_key(cell)
            if not key:
                continue
            # Longest match wins so a fund whose name contains a sibling's
            # name is not mistaken for it.
            match = max(
                (k for k in keyed if k and k in key), key=len, default=None
            )
            if match:
                mapping[column] = keyed[match]
        if len(mapping) > len(best):
            best, best_row = mapping, row_index
    return best, best_row


def _single_fund_columns(table: Table, document: str, series_id: str) -> Dict[int, str]:
    """Column map for a statement that covers exactly one fund.

    Filings that give each fund its own section (Guardian VP Trust) name the
    fund in a banner rather than in the table header, so there is nothing for
    ``_column_map`` to match on.

    Only the first numeric column is mapped. Statements of Changes in Net Assets
    routinely place the prior period in a second column, and attributing both to
    the same fund would silently double the fund's figures. Capturing
    comparative periods needs its own column semantics; until then they are left
    out rather than guessed at.
    """
    grid = table.grid(document)
    for row in grid:
        numeric = [i for i, cell in enumerate(row) if normalize_value(cell) is not None]
        if numeric:
            return {numeric[0]: series_id}
    return {}


def extract_line_items(
    document: ParsedDocument,
    series: Dict[str, str],
    start: int,
    end: int,
    section_series: Sequence[str] = (),
) -> List[LineItem]:
    """Pull line items from every statement table in a character range.

    ``section_series`` is the attribution for the enclosing section, used only
    when the table header names no fund.
    """
    items: List[LineItem] = []
    for table in tables_for(document, start, end):
        mapping, header_row = _column_map(table, document.text, series)
        if not mapping and len(section_series) == 1:
            mapping = _single_fund_columns(table, document.text, section_series[0])
            header_row = -1
        if not mapping:
            continue  # names no known fund and the section is ambiguous: skip

        grid = table.grid(document.text)
        for row_index, row in enumerate(grid):
            if row_index <= header_row:
                continue
            caption = next((c.strip() for c in row if c.strip()), "")
            if not caption or normalize_value(caption) is not None:
                continue  # blank row, or a stray numeric with no caption

            cells = table.rows[row_index].cells if row_index < len(table.rows) else []
            for column, series_id in mapping.items():
                if column >= len(row):
                    continue
                value = normalize_value(row[column])
                if value is None:
                    continue
                cell = _cell_at(table, row_index, column)
                items.append(
                    LineItem(
                        series_id=series_id,
                        caption=caption,
                        value=value,
                        column_index=column,
                        char_start=cell.start if cell else table.start,
                        char_end=cell.end if cell else table.end,
                    )
                )
    return items


def _cell_at(table: Table, row_index: int, column: int):
    """Resolve a grid column back to the cell that produced it."""
    if row_index >= len(table.rows):
        return None
    position = 0
    for cell in table.rows[row_index].cells:
        if position <= column < position + cell.colspan:
            return cell
        position += cell.colspan
    return None
