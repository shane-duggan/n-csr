"""Extract the fair-value hierarchy table.

Every schedule of investments is followed by a summary stating, per asset class,
how much of the fund's value sits in Level 1, 2 and 3 of the fair-value
hierarchy. That table is the authoritative source for "which funds hold the most
Level 3 securities": it is disclosed directly, in the filing's own arithmetic,
rather than inferred by resolving per-security footnote symbols.

It is also self-checking. Levels must sum to the stated total on every row, so a
misread column is caught rather than silently believed.

A dash is a disclosed zero, not a missing value. "Level 3: —" is the fund
affirming it holds nothing at Level 3, which is a different fact from not
having said, and the distinction matters to a reviewer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .htmltables import ParsedDocument, Table
from .statements import normalize_value

#: A header cell naming a hierarchy level.
_LEVEL = re.compile(r"Level\s*([123])\b", re.I)

#: The column holding each row's total across levels.
_TOTAL_COLUMN = re.compile(r"\bTotal\b|\bMarket Value\b|\bFair Value\b", re.I)

#: Rows that state a total rather than an asset class.
_TOTAL_ROW = re.compile(r"^\s*Total\b", re.I)
#: The grand total, as distinct from a per-asset-class subtotal.
_GRAND_TOTAL = re.compile(r"^\s*Total\s+(?:Investments|Assets)\b", re.I)

#: Placeholders a filing uses for a disclosed zero.
_DASHES = frozenset({"-", "‐", "‑", "‒", "–", "—", "―"})

#: Rows must reconcile to their stated total within this fraction.
TOLERANCE = 0.005

_HEADER_SCAN_ROWS = 4


def amount(cell: str) -> Optional[str]:
    """Parse a hierarchy cell, treating a dash as a disclosed zero."""
    text = cell.strip()
    if not text:
        return None
    stripped = text.lstrip("$").strip()
    # A trailing footnote marker may follow the figure, e.g. "— (1)".
    stripped = re.sub(r"\(\d{1,2}\)\s*$", "", stripped).strip()
    if stripped and all(character in _DASHES for character in stripped):
        return "0"
    return normalize_value(stripped)


@dataclass(frozen=True)
class LevelAmount:
    """One asset class at one hierarchy level."""

    series_id: str
    category: str
    level: int
    amount: str
    is_total_row: bool = False
    char_start: int = 0
    char_end: int = 0


@dataclass
class Hierarchy:
    """A parsed hierarchy table plus its internal consistency check."""

    amounts: List[LevelAmount]
    #: Rows whose levels did not sum to their stated total.
    discrepancies: List[Tuple[str, float, float]]

    @property
    def is_consistent(self) -> bool:
        return not self.discrepancies

    def total_at(self, level: int) -> float:
        """Fund-wide amount at a level, from the table's own grand-total row.

        A hierarchy table may carry several rows beginning "Total" -- a subtotal
        per asset class as well as the grand total. Taking the first would
        report a subtotal as the fund-wide figure, so "Total Investments" wins
        and the last total row is the fallback.
        """
        totals = [
            a for a in self.amounts if a.level == level and a.is_total_row
        ]
        grand = [a for a in totals if _GRAND_TOTAL.match(a.category)]
        if grand:
            return float(grand[-1].amount)
        if totals:
            return float(totals[-1].amount)
        return sum(
            float(a.amount)
            for a in self.amounts
            if a.level == level and not a.is_total_row
        )


def _level_columns(grid: Sequence[Sequence[str]]) -> Tuple[Dict[int, int], int, int]:
    """Locate the level columns, the total column, and the header row."""
    for row_index, row in enumerate(grid[:_HEADER_SCAN_ROWS]):
        levels: Dict[int, int] = {}
        total_column = -1
        for column, cell in enumerate(row):
            match = _LEVEL.search(cell)
            if match:
                levels[column] = int(match.group(1))
            elif total_column < 0 and _TOTAL_COLUMN.search(cell):
                total_column = column
        # Two distinct levels is enough to identify the table; some filings
        # omit a level column entirely when nothing is held there.
        if len(set(levels.values())) >= 2:
            return levels, total_column, row_index
    return {}, -1, -1


def is_hierarchy_table(table: Table, document: str) -> bool:
    levels, _, _ = _level_columns(table.grid(document))
    return bool(levels)


def extract_hierarchy(
    document: ParsedDocument, series_id: str, start: int, end: int
) -> Optional[Hierarchy]:
    """Parse the fair-value hierarchy table in a character range, if present."""
    for table in document.tables_within(start, end):
        grid = table.grid(document.text)
        levels, total_column, header_row = _level_columns(grid)
        if not levels:
            continue

        amounts: List[LevelAmount] = []
        discrepancies: List[Tuple[str, float, float]] = []

        for row_index, row in enumerate(grid):
            if row_index <= header_row:
                continue
            category = row[0].strip() if row else ""
            if not category or amount(category) is not None:
                continue

            is_total = bool(_TOTAL_ROW.match(category))
            parsed: Dict[int, float] = {}
            for column, level in levels.items():
                if column >= len(row):
                    continue
                value = amount(row[column])
                if value is None:
                    continue
                cell = _cell(table, row_index, column)
                parsed[level] = float(value)
                amounts.append(
                    LevelAmount(
                        series_id=series_id,
                        category=category,
                        level=level,
                        amount=value,
                        is_total_row=is_total,
                        char_start=cell.start if cell else table.start,
                        char_end=cell.end if cell else table.end,
                    )
                )

            # Self-check: the levels must sum to the row's stated total.
            if parsed and 0 <= total_column < len(row):
                stated = amount(row[total_column])
                if stated is not None:
                    expected = float(stated)
                    got = sum(parsed.values())
                    if expected and abs(got - expected) / abs(expected) > TOLERANCE:
                        discrepancies.append((category, got, expected))

        if amounts:
            return Hierarchy(amounts=amounts, discrepancies=discrepancies)
    return None


def _cell(table: Table, row_index: int, column: int):
    if row_index >= len(table.rows):
        return None
    position = 0
    for cell in table.rows[row_index].cells:
        if position <= column < position + cell.colspan:
            return cell
        position += cell.colspan
    return None
