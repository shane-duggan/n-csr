"""Audit-opinion extraction and series-coverage reconciliation.

The coverage check answers "does the audit opinion cover every series in this
filing?" deterministically, with no LLM: extract each substantive opinion, then
test every header series name for presence in it.

The inverted form matters. Parsing the opinion's *own* fund list is brittle --
filings variously enumerate funds after "comprised of", in the addressee line,
or in a trailing table, with inconsistent conjunction grammar. Testing known
names for presence sidesteps all of that and reconciled 101/101 series across 13
filings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Sequence

from .normalize import fund_key

_HEADING = re.compile(r"Report of Independent Registered Public Accounting Firm", re.I)

# Distinguishes a real opinion from a contents-list entry or a page header.
_AUDIT_LANGUAGE = re.compile(r"we have audited|In our opinion", re.I)

_DOT_LEADER = re.compile(r"\.{3,}")
_TOC_LOOKAHEAD = 250

#: Characters after the heading treated as the opinion body. Comfortably covers
#: the addressee block and scope paragraph, where fund names are enumerated.
OPINION_WINDOW = 6000


@dataclass(frozen=True)
class Opinion:
    start: int
    text: str


@dataclass
class Reconciliation:
    """Result of checking audit-opinion coverage against the header roster."""

    opinions: int
    covered: Dict[str, str]
    uncovered: Dict[str, str]

    @property
    def total(self) -> int:
        return len(self.covered) + len(self.uncovered)

    @property
    def is_complete(self) -> bool:
        return self.total > 0 and not self.uncovered


def find_opinions(text: str) -> List[Opinion]:
    """Extract every substantive audit opinion in the document.

    A filing may contain many. Victory Portfolios (0000802716-26-000007)
    concatenates four annual reports with four separate opinions, each covering
    a subset of series; Guardian VP Trust carries 24. Coverage must therefore be
    evaluated as a union across all opinions -- taking only the first reported
    5/15 for Victory.
    """
    opinions = []
    for m in _HEADING.finditer(text):
        if _DOT_LEADER.search(text[m.end() : m.end() + _TOC_LOOKAHEAD]):
            continue  # contents entry
        body = text[m.start() : m.start() + OPINION_WINDOW]
        if _AUDIT_LANGUAGE.search(body):
            opinions.append(Opinion(start=m.start(), text=body))
    return opinions


def reconcile_coverage(
    series: Dict[str, str], opinions: Sequence[Opinion]
) -> Reconciliation:
    """Check that every series in the header is named in some audit opinion.

    An uncovered series is a genuine review finding: the filing's own roster
    lists a fund the auditors did not name.
    """
    keyed = [fund_key(o.text) for o in opinions]
    covered, uncovered = {}, {}
    for sid, name in series.items():
        target = fund_key(name)
        if target and any(target in body for body in keyed):
            covered[sid] = name
        else:
            uncovered[sid] = name
    return Reconciliation(
        opinions=len(opinions), covered=covered, uncovered=uncovered
    )
