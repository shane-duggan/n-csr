"""Locate the Item 7 financial-statement sections within an N-CSR document.

Validated against 15 filings from 9 filing agents. Each tolerance below exists
because a real filing broke without it -- see the comments.

Note that layout does *not* track the filing agent: Penn Series
(0001193125-26-092803) and Guardian VP Trust (0001193125-26-095292) share agent
0001193125 and use different structures entirely. Do not key parsing behaviour
on the agent prefix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Sequence

#: An Item 7 span shorter than this is a cross-reference or stub, not a section.
MIN_SECTION_CHARS = 5000

#: How far past a heading to look for table-of-contents dot leaders.
_TOC_LOOKAHEAD = 250

# A run of dot leaders means the "heading" is really a table-of-contents entry.
# Guggenheim Variable Funds (0001398344-26-004846) embeds a full contents list
# using the same ITEM n: wording, which truncates the span to ~1k chars.
_DOT_LEADER = re.compile(r"\.{3,}")


def _item_heading(number: int, keyword: str) -> re.Pattern:
    """Build a tolerant matcher for an N-CSR item heading.

    Tolerances, each traced to a filing:
      * case-insensitive -- Templeton Institutional Funds (0001133228-26-002516)
        writes headings in ALL CAPS.
      * separator class ``. : - en-dash em-dash`` or nothing -- BlackRock Series
        Fund (0000319108-26-000003) writes ``Item 7 - Financial Statements``.
      * up to 15 filler characters before the keyword, and a truncated keyword
        stem -- Templeton's heading contains the typo ``FINANCIAL HIGLIGHTS``.
    """
    return re.compile(
        r"Item\s*%d\s*[\.\:\-–—]?\s*(?=[A-Z])(?:.{0,15}?)%s" % (number, keyword),
        re.I,
    )


_H7 = _item_heading(7, r"Financial\s+Statements\s+and\s+Financial\s+Hig")
_H8 = _item_heading(8, r"Changes\s+in\s+and\s+Disagreements")

#: Holdings-schedule headings. Victory Portfolios uses "Schedule of Portfolio
#: Investments" and has zero occurrences of the common spelling; Voya uses
#: "Portfolio of Investments"; Blackstone uses the Consolidated form.
SCHEDULE_HEADINGS = re.compile(
    r"(Consolidated\s+)?(Schedules?|Portfolios?)\s+of\s+(Portfolio\s+)?Investments",
    re.I,
)


@dataclass(frozen=True)
class Span:
    """A half-open ``[start, end)`` character range in the normalized text."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start

    def slice(self, text: str) -> str:
        return text[self.start : self.end]


def _heading_offsets(pattern: re.Pattern, text: str) -> List[int]:
    """Match offsets for a heading, excluding table-of-contents entries."""
    return [
        m.start()
        for m in pattern.finditer(text)
        if not _DOT_LEADER.search(text[m.end() : m.end() + _TOC_LOOKAHEAD])
    ]


#: Front matter that opens a report. Many filings also print this as a per-page
#: header link, so it is only treated as a boundary inside a span already known
#: to hold several reports.
_REPORT_FRONT_MATTER = re.compile(r"Table\s+of\s+Contents", re.I)


def split_reports(text: str, span: Span, opinion_offsets: Sequence[int]) -> List[Span]:
    """Split a span that concatenates several complete reports.

    Victory Portfolios packs four annual reports into one Item 7 span, each with
    its own contents page, audit opinion, and back matter. Because a section
    runs from its heading to the next one, the opinion at the end of one report
    absorbs 20-34k characters of the next report's front matter, which wrecks
    attribution for everything downstream.

    The split is gated on the span containing more than one substantive audit
    opinion, which is what actually distinguishes a multi-report span. Without
    that gate this would shatter ordinary filings: Penn Series prints "Table of
    Contents" as a page header on every page, and splitting there would break
    the continuation carry-forward that holds a paginated schedule together.
    """
    inside = [o for o in opinion_offsets if span.start <= o < span.end]
    if len(inside) < 2:
        return [span]

    boundaries = [
        m.start()
        for m in _REPORT_FRONT_MATTER.finditer(text, span.start, span.end)
        # Front matter only marks a new report if a report already ended.
        if any(o < m.start() for o in inside)
    ]
    if not boundaries:
        return [span]

    cuts = [span.start] + sorted(set(boundaries)) + [span.end]
    parts = [
        Span(a, b) for a, b in zip(cuts, cuts[1:]) if b - a >= MIN_SECTION_CHARS
    ]
    return parts or [span]


def find_item7_spans(text: str) -> List[Span]:
    """Return every substantive Item 7 section in a normalized text stream.

    Usually one span. Filings that concatenate a complete Item 1-11 block per
    fund yield one span per fund -- Guardian VP Trust produces exactly 24, one
    per series.

    Each Item 7 heading is paired with the next Item 8 heading. Where several
    headings resolve to the same end offset (an Item 6 cross-reference sitting a
    few hundred characters before the real Item 7 heading), the widest span
    wins.
    """
    starts = _heading_offsets(_H7, text)
    ends = _heading_offsets(_H8, text)

    widest_by_end = {}
    for start in starts:
        following = [e for e in ends if e > start]
        if not following:
            continue
        end = following[0]
        if end - start < MIN_SECTION_CHARS:
            continue
        if end not in widest_by_end or start < widest_by_end[end]:
            widest_by_end[end] = start

    return sorted(
        (Span(start, end) for end, start in widest_by_end.items()),
        key=lambda s: s.start,
    )
