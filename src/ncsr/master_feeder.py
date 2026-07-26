"""Detect master-feeder relationships.

A feeder fund invests all of its assets in a master portfolio, and the master's
holdings are reported inside the feeder's filing as well as in the master's own
N-CSR. Both filings therefore describe the same securities, and a naive
cross-fund aggregate double counts them.

Resolution policy is **look-through**: the feeder is credited with the
securities, because the feeder is the registered fund under review. Series
identified as master portfolios are excluded from default holdings aggregates
and remain queryable by opting in.

Feeders state the relationship in a fixed sentence:

    ... iShares S&P 500 Index Fund (the "Fund") for the period of January 1,
    2025 ... The Fund invests all of its assets in the S&P 500 Index Master
    Portfolio (the "Master Portfolio"), a series of Master Investment Portfolio.

Rather than parse that free text (the leading clause varies), this module
anchors on the master-side phrase and then scans backwards for a *known* series
name from the filing's own roster -- the same answer-key approach used for
per-fund attribution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .normalize import fund_key

#: The master-side declaration. Captures the master portfolio's name.
_INVESTS_IN = re.compile(
    r"invests?\s+(?:all|substantially\s+all)\s+of\s+its\s+(?:net\s+)?assets\s+in\s+"
    r"(?:the\s+)?(?P<master>[A-Z][^(]{3,80}?)\s*\(\s*the\s*[“\"']?Master",
    re.I,
)

#: How far back to look for the feeder's own name.
_LOOKBACK = 320

#: Series whose name marks them as a pooling vehicle rather than a retail fund.
_MASTER_NAME = re.compile(r"\bMaster\s+(?:Portfolio|Fund)\b", re.I)


@dataclass
class MasterFeeder:
    """Master-feeder structure detected in one filing."""

    #: series_id -> master portfolio name, for feeders in this filing's roster.
    feeders: Dict[str, str] = field(default_factory=dict)
    #: series_ids in this filing that are themselves master portfolios.
    masters: List[str] = field(default_factory=list)
    #: Master names declared by feeders that could not be tied to a series.
    unresolved: List[str] = field(default_factory=list)

    @property
    def is_master_filing(self) -> bool:
        return bool(self.masters)

    @property
    def has_structure(self) -> bool:
        return bool(self.feeders or self.masters)

    def excluded_from_aggregates(self) -> List[str]:
        """Series whose holdings must not be counted in default aggregates.

        Under look-through the feeder carries the position, so the master's own
        rows would double count.
        """
        return sorted(self.masters)


def _feeder_before(text: str, position: int, keyed: Dict[str, str], floor: int) -> Optional[str]:
    """Find the nearest preceding series name from this filing's roster."""
    window = text[max(floor, position - _LOOKBACK) : position]
    normalized = fund_key(window)
    best: Optional[str] = None
    best_length = 0
    for key, series_id in keyed.items():
        # Longest match wins, so "iShares S&P 500 Index Fund" beats a shorter
        # sibling whose name is a substring of it.
        if key in normalized and len(key) > best_length:
            best, best_length = series_id, len(key)
    return best


def detect(text: str, series: Dict[str, str], start: int = 0, end: Optional[int] = None) -> MasterFeeder:
    """Detect master-feeder structure for one filing."""
    if end is None:
        end = len(text)

    keyed = {fund_key(name): sid for sid, name in series.items() if fund_key(name)}

    feeders: Dict[str, str] = {}
    unresolved: List[str] = []
    for match in _INVESTS_IN.finditer(text, start, end):
        master_name = match.group("master").strip()
        feeder_id = _feeder_before(text, match.start(), keyed, start)
        if feeder_id is None:
            if master_name not in unresolved:
                unresolved.append(master_name)
            continue
        feeders.setdefault(feeder_id, master_name)

    masters = sorted(
        sid for sid, name in series.items() if _MASTER_NAME.search(name or "")
    )

    # A series cannot be both, and the explicit declaration wins over the
    # name-based heuristic.
    for sid in feeders:
        if sid in masters:
            masters.remove(sid)

    return MasterFeeder(feeders=feeders, masters=masters, unresolved=unresolved)
