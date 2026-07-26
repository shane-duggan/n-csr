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
from typing import Dict, List, Optional, Sequence

from .normalize import fund_key

_HEADING = re.compile(r"Report of Independent Registered Public Accounting Firm", re.I)

# Distinguishes a real opinion from a contents-list entry or a page header.
_AUDIT_LANGUAGE = re.compile(r"we have audited|In our opinion", re.I)

_DOT_LEADER = re.compile(r"\.{3,}")
_TOC_LOOKAHEAD = 250

#: Characters after the heading treated as the opinion body. A fixed 6,000 was
#: too tight: KraneShares enumerates 25 funds and the list runs past it, so nine
#: series read as uncovered when the opinion in fact names them. The window is
#: now bounded by the *next* opinion instead, which is what the tight limit was
#: really guarding against -- coverage must not stray into a neighbouring
#: report. This value is only the cap when no further opinion follows.
OPINION_WINDOW = 14000

#: A wider window used only for the required-element checks. The signature block
#: sits at the foot of the opinion and Guggenheim's is 10,000 characters past
#: the heading -- far outside the coverage window, but unambiguous once found.
ELEMENT_WINDOW = 14000


#: Elements an opinion must carry, each checkable without a model.
#: The electronic signature line. Firms sign as "/s/ KPMG LLP" and what follows
#: varies (a tenure sentence, a city, nothing), so the match is anchored on the
#: firm-type suffix rather than on whatever comes next.
_SIGNATURE = re.compile(
    r"/s/\s*([A-Z][A-Za-z.,&'\- ]{2,55}?(?:LLP|LLC|L\.L\.P\.|P\.?C\.?|LTD\.?))\b"
)
#: "We have served as the auditor of one or more Penn Series Funds, Inc.
#: investment companies since 2004." -- the fund name routinely contains a
#: period, so the gap cannot exclude them.
_TENURE = re.compile(r"We have served as.{0,200}?since\s+(\d{4})", re.I | re.S)
_CITY_DATE = re.compile(
    r"([A-Z][A-Za-z.\- ]+,\s*[A-Z][A-Za-z.\- ]+)\s+"
    r"((?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2},\s*\d{4})"
)
_PCAOB = re.compile(r"standards of the PCAOB|Public Company Accounting Oversight Board", re.I)


@dataclass(frozen=True)
class Opinion:
    start: int
    text: str
    #: Wider slice used only for required-element checks; see ELEMENT_WINDOW.
    element_text: str = ""

    @property
    def _elements(self) -> str:
        return self.element_text or self.text

    @property
    def auditor(self) -> Optional[str]:
        match = _SIGNATURE.search(self._elements)
        return match.group(1).strip() if match else None

    @property
    def auditor_since(self) -> Optional[str]:
        match = _TENURE.search(self._elements)
        return match.group(1) if match else None

    @property
    def report_date(self) -> Optional[str]:
        match = _CITY_DATE.search(self._elements)
        return match.group(2) if match else None

    @property
    def city(self) -> Optional[str]:
        match = _CITY_DATE.search(self._elements)
        return match.group(1) if match else None

    @property
    def cites_pcaob(self) -> bool:
        return bool(_PCAOB.search(self._elements))

    def missing_elements(self) -> List[str]:
        """Required elements absent from the opinion, by name."""
        missing = []
        if not self.auditor:
            missing.append("auditor signature")
        if not self.report_date:
            missing.append("report date")
        if not self.city:
            missing.append("city of issuance")
        if not self.auditor_since:
            missing.append("auditor tenure statement")
        if not self.cites_pcaob:
            missing.append("reference to PCAOB standards")
        return missing


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
    starts = []
    for m in _HEADING.finditer(text):
        if _DOT_LEADER.search(text[m.end() : m.end() + _TOC_LOOKAHEAD]):
            continue  # contents entry
        if _AUDIT_LANGUAGE.search(text[m.start() : m.start() + OPINION_WINDOW]):
            starts.append(m.start())

    opinions = []
    for index, start in enumerate(starts):
        # Stop at the next opinion so a filing that concatenates several
        # reports cannot have one opinion's coverage bleed into the next.
        ceiling = starts[index + 1] if index + 1 < len(starts) else len(text)
        body_end = min(start + OPINION_WINDOW, ceiling)
        element_end = min(start + ELEMENT_WINDOW, ceiling)
        opinions.append(
            Opinion(
                start=start,
                text=text[start:body_end],
                element_text=text[start:element_end],
            )
        )
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
