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
from typing import List

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
