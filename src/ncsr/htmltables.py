"""Offset-preserving HTML table parser.

Sectioning and attribution work on the flattened text produced by
``normalize.textify``, and every archived offset is expressed in that
coordinate space. Table extraction needs cell structure, which flattening
destroys -- so this module rebuilds the *same* text while streaming the markup,
recording where each table and cell lands.

The invariant that makes it safe is that ``parse(markup).text`` is identical to
``textify(markup)``, asserted against every filing in the corpus. Offsets
therefore agree by construction rather than by approximate re-matching, and
nothing downstream has to change.

Stdlib ``html.parser`` is used deliberately: it keeps the Lambda package free of
native dependencies, and streaming gives precise control over how the text is
assembled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from html.parser import HTMLParser
from typing import List, Optional, Tuple

from .normalize import unescape_stable

#: Content of these elements is dropped, matching ``textify``.
_OPAQUE = frozenset({"script", "style"})

_CELL_TAGS = frozenset({"td", "th"})

#: Geometry for filings whose financial statements are a PDF-to-HTML
#: conversion: every word is an absolutely positioned div rather than a cell.
_ABSOLUTE = re.compile(r"position\s*:\s*absolute", re.I)
_TOP = re.compile(r"(?:^|;)\s*top\s*:\s*(-?[\d.]+)px", re.I)
_LEFT = re.compile(r"(?:^|;)\s*left\s*:\s*(-?[\d.]+)px", re.I)
_PAGE_BREAK = re.compile(r"page-break-(?:after|before)", re.I)

#: Tags that implicitly close an open cell or row when the markup omits the
#: closing tag, which filings do constantly.
_IMPLICIT_CLOSERS = frozenset({"tr", "td", "th", "table"})


@dataclass
class Box:
    """An absolutely positioned text fragment.

    ``page`` scopes the coordinates: each converted page restarts at the
    origin, so ``top`` alone would merge unrelated lines from different pages.
    """

    page: int
    top: float
    left: float
    start: int
    end: int = 0
    children: int = 0

    @property
    def is_leaf(self) -> bool:
        """Leaf boxes hold the text; the rest are page and column containers."""
        return self.children == 0

    def text(self, document: str) -> str:
        return document[self.start : self.end]


@dataclass
class Cell:
    start: int
    end: int = 0
    colspan: int = 1
    rowspan: int = 1
    header: bool = False

    def text(self, document: str) -> str:
        return document[self.start : self.end].strip()


@dataclass
class Row:
    cells: List[Cell] = field(default_factory=list)


@dataclass
class Table:
    start: int
    end: int = 0
    rows: List[Row] = field(default_factory=list)
    #: Nesting depth; filings use tables for page layout, so the outermost
    #: table is usually furniture and the data lives deeper.
    depth: int = 0

    @property
    def width(self) -> int:
        return max((sum(c.colspan for c in r.cells) for r in self.rows), default=0)

    @property
    def height(self) -> int:
        return len(self.rows)

    def grid(self, document: str) -> List[List[str]]:
        """Cell text expanded across ``colspan``.

        ``rowspan`` is not propagated: in financial statements it is used for
        stub headings rather than data, and inventing values would fabricate
        rows that were never filed.
        """
        out: List[List[str]] = []
        for row in self.rows:
            line: List[str] = []
            for cell in row.cells:
                value = cell.text(document)
                line.append(value)
                line.extend([""] * (cell.colspan - 1))
            out.append(line)
        return out


class _Collector(HTMLParser):
    """Rebuilds the normalized text while recording table geometry."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._pieces: List[str] = []
        self._length = 0
        self._pending_space = False
        self._opaque_depth = 0
        self._table_stack: List[Table] = []
        self.tables: List[Table] = []
        self._box_stack: List[Box] = []
        self._page = 0
        self.boxes: List[Box] = []

    # -- text assembly ----------------------------------------------------

    def _emit(self, chunk: str) -> None:
        """Append text, collapsing whitespace runs across chunk boundaries."""
        if not chunk:
            return
        text = unescape_stable(chunk)
        buffer: List[str] = []
        for char in text:
            if char.isspace():
                self._pending_space = True
                continue
            if self._pending_space:
                self._pending_space = False
                if self._length or buffer:
                    buffer.append(" ")
            buffer.append(char)
        if buffer:
            joined = "".join(buffer)
            self._pieces.append(joined)
            self._length += len(joined)

    def _space(self) -> None:
        """A tag contributes a single space, exactly as ``textify`` does."""
        if self._length:
            self._pending_space = True

    @property
    def text(self) -> str:
        return "".join(self._pieces)

    # -- structure --------------------------------------------------------

    def _close_cell(self) -> None:
        if not self._table_stack:
            return
        table = self._table_stack[-1]
        if table.rows and table.rows[-1].cells:
            cell = table.rows[-1].cells[-1]
            if not cell.end:
                cell.end = self._length

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _OPAQUE:
            self._opaque_depth += 1
        self._space()

        if tag == "div":
            style = dict(attrs).get("style") or ""
            if _PAGE_BREAK.search(style):
                self._page += 1
            if _ABSOLUTE.search(style):
                top = _TOP.search(style)
                left = _LEFT.search(style)
                box = Box(
                    page=self._page,
                    top=float(top.group(1)) if top else 0.0,
                    left=float(left.group(1)) if left else 0.0,
                    start=self._length,
                )
                parent = next(
                    (b for b in reversed(self._box_stack) if b is not None), None
                )
                if parent is not None:
                    parent.children += 1
                self._box_stack.append(box)
                self.boxes.append(box)
            else:
                self._box_stack.append(None)

        if tag == "table":
            table = Table(start=self._length, depth=len(self._table_stack))
            self._table_stack.append(table)
            self.tables.append(table)
        elif self._table_stack:
            if tag == "tr":
                self._close_cell()
                self._table_stack[-1].rows.append(Row())
            elif tag in _CELL_TAGS:
                self._close_cell()
                table = self._table_stack[-1]
                if not table.rows:
                    table.rows.append(Row())
                values = dict(attrs)
                table.rows[-1].cells.append(
                    Cell(
                        start=self._length,
                        colspan=_span(values.get("colspan")),
                        rowspan=_span(values.get("rowspan")),
                        header=tag == "th",
                    )
                )

    def handle_endtag(self, tag: str) -> None:
        if tag in _OPAQUE and self._opaque_depth:
            self._opaque_depth -= 1
        if tag in _IMPLICIT_CLOSERS:
            self._close_cell()
        self._space()
        if tag == "div" and self._box_stack:
            box = self._box_stack.pop()
            if box is not None:
                box.end = self._length
        if tag == "table" and self._table_stack:
            self._table_stack.pop().end = self._length

    def handle_startendtag(self, tag: str, attrs) -> None:
        self._space()

    def handle_data(self, data: str) -> None:
        if self._opaque_depth:
            return
        self._emit(data)

    def handle_comment(self, data: str) -> None:
        self._space()

    def handle_decl(self, decl: str) -> None:
        self._space()

    def handle_pi(self, data: str) -> None:
        self._space()

    def unknown_decl(self, data: str) -> None:
        self._space()


def _span(value: Optional[str]) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return 1
    return parsed if 1 <= parsed <= 100 else 1


@dataclass
class ParsedDocument:
    text: str
    tables: List[Table]
    boxes: List[Box] = field(default_factory=list)

    def boxes_within(self, start: int, end: int) -> List[Box]:
        """Leaf text fragments fully inside a character range."""
        return [
            b
            for b in self.boxes
            if b.is_leaf and b.end and b.start >= start and b.end <= end
        ]

    def tables_within(self, start: int, end: int) -> List[Table]:
        """Tables fully contained in a character range."""
        return [t for t in self.tables if t.start >= start and t.end <= end and t.end]


def parse(markup: str) -> ParsedDocument:
    """Parse markup into normalized text plus table geometry."""
    collector = _Collector()
    collector.feed(markup)
    collector.close()
    for table in collector.tables:
        if not table.end:
            table.end = collector._length
    for box in collector.boxes:
        if not box.end:
            box.end = collector._length
    return ParsedDocument(
        text=collector.text, tables=collector.tables, boxes=collector.boxes
    )
