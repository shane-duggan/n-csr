"""Reconstruct tables from absolutely positioned text.

Four of fifteen filings deliver their financial statements as a PDF-to-HTML
conversion: every word is its own ``position:absolute`` div and there is no
``<table>`` anywhere. BlackRock uses 92,357 divs against 1,750 cells.

Rather than write a second extraction path, this module synthesizes ordinary
``Table`` objects from the geometry, so ``statements``, ``holdings`` and
``fairvalue`` work against div-laid-out filings unchanged.

Three properties of the layout do the work:

*Lines* are fragments sharing a vertical position, within a tolerance --
superscript footnote markers sit a few pixels below their line and must join it
rather than forming a line of their own.

*Dot leaders* separate a description from its figures. They are the column
separator this layout has, and they are unambiguous.

*Pages are two-up.* One physical line carries two holdings side by side, so a
line yields a record per dot-leader run rather than one record per line.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

from .htmltables import Box, Cell, ParsedDocument, Row, Table
from .normalize import normalize_value

#: Fragments within this many pixels vertically belong to the same line. The
#: coordinate space is scaled up (a body line is ~130px tall), so this is a
#: fraction of a line rather than a loose guess.
LINE_TOLERANCE = 12.0

#: A run of dots acting as a column separator.
_DOT_LEADER = re.compile(r"^[\s.·•]{3,}$")

#: A trailing footnote marker, which follows a description rather than starting
#: a new one.
_MARKER = re.compile(r"^\(?[a-z0-9]{1,3}\)$", re.I)

#: A currency symbol sits between a figure group's members and must not be read
#: as the first word of the next record.
_CURRENCY = re.compile(r"^[$€£¥]$")


def _is_dots(text: str) -> bool:
    return bool(_DOT_LEADER.match(text)) and text.count(".") >= 3


def _lines(boxes: Sequence[Box]) -> List[List[Box]]:
    """Group fragments into visual lines, page by page."""
    by_page: Dict[int, List[Box]] = defaultdict(list)
    for box in boxes:
        by_page[box.page].append(box)

    lines: List[List[Box]] = []
    for page in sorted(by_page):
        current: List[Box] = []
        anchor: Optional[float] = None
        for box in sorted(by_page[page], key=lambda b: (b.top, b.left)):
            if anchor is None or abs(box.top - anchor) <= LINE_TOLERANCE:
                if anchor is None:
                    anchor = box.top
                current.append(box)
            else:
                lines.append(sorted(current, key=lambda b: b.left))
                current = [box]
                anchor = box.top
        if current:
            lines.append(sorted(current, key=lambda b: b.left))
    return lines


def _records(line: Sequence[Box], document: str) -> List[Tuple[List[Box], List[Box]]]:
    """Split one visual line into (description, figures) pairs.

    A two-up page yields two pairs. Figures run until a non-numeric fragment
    appears, which begins the next record's description.
    """
    records: List[Tuple[List[Box], List[Box]]] = []
    description: List[Box] = []
    figures: List[Box] = []
    after_dots = False

    for box in line:
        text = document[box.start : box.end].strip()
        if not text:
            continue
        if _is_dots(text):
            after_dots = True
            continue
        if after_dots:
            if _CURRENCY.match(text):
                continue
            if normalize_value(text) is not None:
                figures.append(box)
                continue
            if _MARKER.match(text) and not figures:
                continue  # a marker trailing the description
            # A word after the figures opens the next record.
            if description:
                records.append((description, figures))
            description, figures, after_dots = [box], [], False
        else:
            description.append(box)

    if description and figures:
        records.append((description, figures))
    return records


def _cell(boxes: Sequence[Box]) -> Cell:
    return Cell(start=boxes[0].start, end=boxes[-1].end)


def synthesize(
    document: ParsedDocument, start: int, end: int
) -> List[Table]:
    """Build ``Table`` objects from positioned text in a character range.

    One table per page, so that column detection and category tracking operate
    over a coherent region rather than the whole filing.
    """
    boxes = document.boxes_within(start, end)
    if not boxes:
        return []

    tables: List[Table] = []
    by_page: Dict[int, List[List[Box]]] = defaultdict(list)
    for line in _lines(boxes):
        if line:
            by_page[line[0].page].append(line)

    for page in sorted(by_page):
        rows: List[Row] = []
        for line in by_page[page]:
            for description, figures in _records(line, document.text):
                cells = [_cell(description)] + [_cell([f]) for f in figures]
                rows.append(Row(cells=cells))
        if not rows:
            continue
        spans = [c for row in rows for c in row.cells]
        tables.append(
            Table(
                start=min(c.start for c in spans),
                end=max(c.end for c in spans),
                rows=rows,
            )
        )
    return tables


def tables_for(document: ParsedDocument, start: int, end: int) -> List[Table]:
    """Tables in a range, synthesizing from geometry when there is no markup.

    A filing uses one layout or the other, so real tables win when present and
    geometry is the fallback. Downstream extraction sees ``Table`` either way.
    """
    real = document.tables_within(start, end)
    if real:
        return real
    return synthesize(document, start, end)
