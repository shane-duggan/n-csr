"""Attribute Item 7 content to individual funds (series).

Filings place the fund name inconsistently relative to the section heading:
after it (Penn Series, BlackRock, Templeton), before it (Kennedy/IMST, Northern
Lights), or in a separate banner (Guardian VP Trust). Rather than enumerate
layouts and guess which applies, this module uses the SGML series roster as an
answer key: find every section heading, then look for *any* known fund name in a
window around it, on either side.

A heading may name zero funds (trust-wide notes), one fund (the usual case), or
several (columnar statements -- Penn's Statement of Operations carries four
funds side by side). All three are represented, so downstream code never has to
pretend a shared statement belongs to one fund.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

from .normalize import fund_key

#: Characters scanned either side of a heading when looking for fund names.
#: Wide enough to clear a trust name and a date ("Penn Series Funds, Inc.
#: Schedule of Investments - December 31, 2025 | Money Market Fund"), narrow
#: enough to exclude neighbouring prose.
WINDOW_BEFORE = 240
WINDOW_AFTER = 240

# Canonical section types. Order matters: the first match wins, so more specific
# patterns precede the general ones ("Notes to Consolidated Financial
# Statements" must beat a bare "Financial Statements").
SECTION_TYPES: Sequence[Tuple[str, re.Pattern]] = (
    (
        "notes_to_financial_statements",
        re.compile(r"Notes\s+to\s+(?:the\s+)?(?:Consolidated\s+)?Financial\s+Statements", re.I),
    ),
    (
        "report_of_independent_registered_public_accounting_firm",
        re.compile(r"Report\s+of\s+Independent\s+Registered\s+Public\s+Accounting\s+Firm", re.I),
    ),
    (
        "schedule_of_investments",
        re.compile(
            r"(?:Consolidated\s+)?(?:Schedules?|Portfolios?)\s+of\s+"
            r"(?:Portfolio\s+)?Investments",
            re.I,
        ),
    ),
    (
        "statement_of_assets_and_liabilities",
        re.compile(r"(?:Consolidated\s+)?Statements?\s+of\s+Assets\s+and\s+Liabilities", re.I),
    ),
    (
        "statement_of_changes_in_net_assets",
        re.compile(r"(?:Consolidated\s+)?Statements?\s+of\s+Changes\s+in\s+Net\s+Assets", re.I),
    ),
    (
        "statement_of_cash_flows",
        re.compile(r"(?:Consolidated\s+)?Statements?\s+of\s+Cash\s+Flows", re.I),
    ),
    (
        "statement_of_operations",
        re.compile(r"(?:Consolidated\s+)?Statements?\s+of\s+Operations", re.I),
    ),
    ("financial_highlights", re.compile(r"Financial\s+Highlights", re.I)),
)

_ANY_HEADING = re.compile(
    "|".join("(?:%s)" % pattern.pattern for _, pattern in SECTION_TYPES), re.I
)

# These phrases appear in running prose far more often than as headings -- in
# audit opinions ("...the financial highlights for each of the five years..."),
# in note cross-references ("...gain/(loss) from investments on the Statement of
# Operations."), and in footnote legends ("Statement of Operations location:").
# Treating those as section boundaries fragments the real sections around them.
#
# After whitespace flattening the only structural signal left is the
# surrounding case: a heading is not preceded by a lowercase word, and is not
# followed by one.
# The lowercase run must be a *whole* word: "...Growth Fund SCHEDULE OF
# INVESTMENTS" is a heading even though "Fund" ends in lowercase letters.
_PROSE_BEFORE = re.compile(r"(?:(?:^|\s)[a-z]{2,}|,)\s*$")

# A cross-reference to another section, not a heading for one. Filings print
# "See notes to financial statements." as a footer on every page of a schedule,
# and it is capitalized, so case alone does not distinguish it. Reading those
# footers as headings gave each schedule page a tiny section followed by a notes
# section that swallowed the holdings -- Victory's Item 7 came out 78% notes and
# 4% schedules, with the securities inside the notes.
_CROSS_REFERENCE = re.compile(
    r"(?:^|\s)(?:see|refer\s+to|in|per|the)\s+(?:the\s+)?(?:accompanying\s+)?$",
    re.I,
)

# A heading may be followed by a lowercase connector before its date
# ("Statement of Assets and Liabilities as of December 31, 2025"), so only a
# lowercase word that is *not* such a connector signals prose.
# Only date connectors are exempt. "for" must stay out: the audit opinion's
# "...the financial highlights for each of the five years..." is prose, and
# genuine headings capitalize it ("Statement of Operations For the Year Ended").
_PROSE_AFTER = re.compile(r"^\s*(?!as\b|at\b)[a-z]{2,}")


def _is_heading(text: str, match: re.Match, floor: int, ceiling: int) -> bool:
    before = text[max(floor, match.start() - 40) : match.start()]
    after = text[match.end() : min(ceiling, match.end() + 40)]
    if _CROSS_REFERENCE.search(before):
        return False
    return not (_PROSE_BEFORE.search(before) or _PROSE_AFTER.match(after))


@dataclass(frozen=True)
class Marker:
    """A section heading and the funds named around it."""

    offset: int
    section_type: str
    series_ids: Tuple[str, ...]

    @property
    def is_shared(self) -> bool:
        """True for a columnar statement covering several funds at once."""
        return len(self.series_ids) > 1

    @property
    def is_unattributed(self) -> bool:
        """True for trust-wide content such as notes."""
        return not self.series_ids


@dataclass(frozen=True)
class FundSection:
    """A contiguous run of Item 7 attributed to zero or more funds."""

    start: int
    end: int
    section_type: str
    series_ids: Tuple[str, ...]

    @property
    def length(self) -> int:
        return self.end - self.start


#: Sections that legitimately belong to the trust rather than to any one fund.
#: Excluded from the attribution-quality metric -- leaving them unattributed is
#: correct, not a miss.
TRUSTWIDE_SECTIONS = frozenset(
    {
        "notes_to_financial_statements",
        "report_of_independent_registered_public_accounting_firm",
        "unknown",
    }
)


@dataclass
class Attribution:
    """Per-fund sections for one Item 7 span, plus coverage diagnostics."""

    sections: List[FundSection] = field(default_factory=list)
    #: series_id -> section types found for it
    by_series: Dict[str, set] = field(default_factory=dict)
    unattributed_chars: int = 0

    def series_with(self, section_type: str) -> set:
        return {
            sid for sid, types in self.by_series.items() if section_type in types
        }

    @property
    def fund_specific_chars(self) -> int:
        return sum(
            s.length
            for s in self.sections
            if s.section_type not in TRUSTWIDE_SECTIONS
        )

    @property
    def attributed_chars(self) -> int:
        return sum(
            s.length
            for s in self.sections
            if s.section_type not in TRUSTWIDE_SECTIONS and s.series_ids
        )


def _classify_heading(text: str) -> str:
    for name, pattern in SECTION_TYPES:
        if pattern.match(text):
            return name
    return "unknown"


def _drop_subsumed(matches: List[Tuple[str, str]]) -> Tuple[str, ...]:
    """Drop a fund whose key is contained in another matched fund's key.

    Guards against a short name matching inside a longer sibling (e.g. "Value
    Fund" inside "Small Cap Value Fund") and attributing a section to both.
    """
    keys = {sid: key for sid, key in matches}
    keep = []
    for sid, key in matches:
        if any(other != key and key in other for other in keys.values()):
            continue
        keep.append(sid)
    return tuple(sorted(set(keep)))


def find_markers(text: str, series: Dict[str, str], start: int, end: int) -> List[Marker]:
    """Locate section headings within ``[start, end)`` and attribute each."""
    keyed = [(sid, fund_key(name)) for sid, name in series.items()]
    keyed = [(sid, key) for sid, key in keyed if key]

    markers: List[Marker] = []
    for match in _ANY_HEADING.finditer(text, start, end):
        if not _is_heading(text, match, start, end):
            continue
        window = text[
            max(start, match.start() - WINDOW_BEFORE) : min(
                end, match.end() + WINDOW_AFTER
            )
        ]
        normalized = fund_key(window)
        hits = [(sid, key) for sid, key in keyed if key in normalized]
        markers.append(
            Marker(
                offset=match.start(),
                section_type=_classify_heading(match.group(0)),
                series_ids=_drop_subsumed(hits),
            )
        )
    return markers


def _carry_forward(markers: Sequence[Marker]) -> List[Marker]:
    """Let a continuation page inherit the fund from the page before it.

    A holdings schedule spanning several pages repeats its heading, but only the
    first page tends to carry the fund name -- later pages read "Schedule of
    Investments (continued)". Without this, one logical section fragments into
    attributed and unattributed pieces.

    Inheritance is deliberately narrow: only an unattributed marker whose
    section type matches the immediately preceding marker inherits from it. A
    different section type in between (trust-wide notes, say) stops the run.
    """
    resolved: List[Marker] = []
    for marker in markers:
        if marker.series_ids or not resolved:
            resolved.append(marker)
            continue
        previous = resolved[-1]
        if previous.section_type == marker.section_type and previous.series_ids:
            marker = Marker(
                offset=marker.offset,
                section_type=marker.section_type,
                series_ids=previous.series_ids,
            )
        resolved.append(marker)
    return resolved


def _collapse(markers: Sequence[Marker]) -> List[Marker]:
    """Merge consecutive markers that describe the same logical section.

    Holdings schedules repeat their heading on every page, which would otherwise
    yield hundreds of single-page sections. Penn Series alone repeats it 115
    times across 29 funds.
    """
    collapsed: List[Marker] = []
    for marker in markers:
        if collapsed:
            previous = collapsed[-1]
            same = (
                previous.section_type == marker.section_type
                and previous.series_ids == marker.series_ids
            )
            if same:
                continue
        collapsed.append(marker)
    return collapsed


def attribute(
    text: str, series: Dict[str, str], start: int, end: int
) -> Attribution:
    """Split one Item 7 span into per-fund sections."""
    markers = find_markers(text, series, start, end)

    # A single-fund registrant has nothing to disambiguate, so its documents
    # generally omit the fund name from section headings entirely -- Voya's
    # Item 7 left 96% of content unattributed before this case was handled.
    # With one series there is no ambiguity to resolve.
    if len(series) == 1:
        only = next(iter(series))
        markers = [Marker(m.offset, m.section_type, (only,)) for m in markers]
    else:
        # Filings that concatenate one complete report per fund give each fund
        # its own Item 7 span. When a span names exactly one fund anywhere in
        # it, every section inside belongs to that fund -- Guardian VP Trust's
        # 24 spans are each single-fund, but only their banner headings repeat
        # the name.
        named = {sid for m in markers for sid in m.series_ids}
        if len(named) == 1:
            only = next(iter(named))
            markers = [Marker(m.offset, m.section_type, (only,)) for m in markers]

    markers = _collapse(_carry_forward(markers))

    sections: List[FundSection] = []
    for index, marker in enumerate(markers):
        stop = markers[index + 1].offset if index + 1 < len(markers) else end
        sections.append(
            FundSection(
                start=marker.offset,
                end=stop,
                section_type=marker.section_type,
                series_ids=marker.series_ids,
            )
        )

    by_series: Dict[str, set] = {}
    unattributed = 0
    for section in sections:
        if not section.series_ids:
            unattributed += section.length
        for sid in section.series_ids:
            by_series.setdefault(sid, set()).add(section.section_type)

    return Attribution(
        sections=sections, by_series=by_series, unattributed_chars=unattributed
    )
