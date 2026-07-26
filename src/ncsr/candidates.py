"""Deterministic retrieval of passages for model review.

The LLM stages are expensive per token and the notes are the largest part of a
filing, so what reaches a model is chosen here rather than by sending the
section wholesale. Retrieval is deliberately narrow and recall-biased in that
order: precise triggers, then a sentence-bounded window, then deduplication.

Trigger choice matters more than it looks. A naive keyword set is almost all
noise: "settlement" is overwhelmingly *trade* settlement ("valued at the last
settlement price"), and "claims" appears as an asset class ("loans, trade
claims, sovereign debt"). Guardian produced 213 hits on the naive set, of which
the visible majority concerned swap pricing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

#: Maximum characters of context handed to a model for one passage.
WINDOW = 900

#: Sentences carried past the hit. The judgement usually turns on what comes
#: *after* the trigger: an indemnification note is only boilerplate once you
#: reach "the risk of loss to be remote", and cutting the passage at the first
#: full stop hides exactly the sentence that decides it.
SENTENCES_AFTER = 3

#: Passages sharing this many leading characters are treated as duplicates.
_DEDUPE_PREFIX = 120


@dataclass(frozen=True)
class Passage:
    """A retrieved excerpt with the offsets needed to cite it."""

    topic: str
    text: str
    start: int
    end: int
    section_type: str
    series_id: str = ""

    @property
    def length(self) -> int:
        return self.end - self.start


#: Topic -> trigger. Each is written to avoid a known false friend:
#:   * "settlement" only counts beside legal language, never on its own;
#:   * "claims" must be claims *against* someone, not "trade claims";
#:   * "proceeding" must be legal, not a procedural aside.
TRIGGERS: Sequence[Tuple[str, "re.Pattern"]] = (
    ("indemnification", re.compile(r"indemnif\w*", re.I)),
    ("contingency", re.compile(r"contingenc\w*|contingent liabilit\w*", re.I)),
    ("litigation", re.compile(r"litigat\w*|lawsuit\w*|legal proceeding\w*", re.I)),
    (
        "claims",
        re.compile(r"claims?\s+(?:against|asserted|brought|filed)", re.I),
    ),
    (
        "legal_settlement",
        re.compile(r"settle\w*\s+(?:of\s+)?(?:the\s+)?(?:litigation|lawsuit|claim|action)", re.I),
    ),
    ("accrual", re.compile(r"accrued\s+(?:liabilit|legal|contingent)\w*", re.I)),
    ("regulatory", re.compile(r"\bSEC\s+(?:investigation|inquiry|order)|regulatory\s+action", re.I)),
)

#: Phrases that share a trigger word but mean something else entirely. An
#: expense-cap clause reads "excluding ... litigation and extraordinary
#: expenses", which is a fee definition rather than a legal proceeding; it
#: accounted for 16 of Guardian's 19 retrieved passages.
EXCLUSIONS = re.compile(
    r"litigation and extraordinary expenses"
    r"|extraordinary and litigation expenses"
    r"|litigation expenses\)",
    re.I,
)

#: Boilerplate that appears in nearly every filing. Retrieved anyway -- the
#: model still has to judge it -- but flagged so a caller can weight or skip it.
BOILERPLATE_HINT = re.compile(
    r"risk of loss to be remote|maximum exposure under these arrangements is unknown",
    re.I,
)


def _sentence_window(text: str, position: int, floor: int, ceiling: int) -> Tuple[int, int]:
    """Expand around a hit to sentence boundaries, bounded by ``WINDOW``."""
    left = max(floor, position - WINDOW // 3)
    right = min(ceiling, position + WINDOW)

    start = text.rfind(". ", left, position)
    start = left if start < 0 else start + 2

    end = position
    for _ in range(SENTENCES_AFTER):
        nxt = text.find(". ", end, right)
        if nxt < 0:
            end = right
            break
        end = nxt + 1
    return start, end


def retrieve(
    text: str,
    sections: Sequence,
    section_types: Sequence[str] = ("notes_to_financial_statements",),
    topics: Sequence[str] = (),
) -> List[Passage]:
    """Collect passages worth a model's attention.

    Deduplicates on the opening of each passage: a filing that repeats the same
    indemnification note once per fund would otherwise pay for the same
    judgement many times over.
    """
    wanted = set(topics) if topics else {name for name, _ in TRIGGERS}
    seen: Dict[str, None] = {}
    passages: List[Passage] = []

    for section in sections:
        if section.section_type not in section_types:
            continue
        series_id = (
            section.series_ids[0] if len(getattr(section, "series_ids", ())) == 1 else ""
        )
        for topic, pattern in TRIGGERS:
            if topic not in wanted:
                continue
            for match in pattern.finditer(text, section.start, section.end):
                start, end = _sentence_window(
                    text, match.start(), section.start, section.end
                )
                excerpt = text[start:end].strip()
                if len(excerpt) < 60 or EXCLUSIONS.search(excerpt):
                    continue
                key = excerpt[:_DEDUPE_PREFIX]
                if key in seen:
                    continue
                seen[key] = None
                passages.append(
                    Passage(
                        topic=topic,
                        text=excerpt,
                        start=start,
                        end=end,
                        section_type=section.section_type,
                        series_id=series_id,
                    )
                )
    return passages


def looks_like_boilerplate(passage: Passage) -> bool:
    return bool(BOILERPLATE_HINT.search(passage.text))
