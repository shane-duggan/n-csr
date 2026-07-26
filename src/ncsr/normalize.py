"""Text and name normalization.

Every rule here traces to a real filing that broke without it; the filing is
named in the comment so the rule can be re-justified (or deleted) later.
"""

from __future__ import annotations

import html
import re

# `VY(R) BrandywineGLOBAL` in the SGML header vs `VY ® BrandywineGLOBAL` in the
# document body (Voya Variable Insurance Trust, 0001104659-26-025188).
_TRADEMARK = re.compile(r"\(r\)|\(sm\)|\(tm\)|®|™|℠", re.I)

# Two spellings seen in the wild:
#   "Victory Low Duration Bond Fund, formerly Victory INCORE Low Duration Bond Fund"
#   "Guardian Small Cap Value Diversified VIP Fund (formerly, Guardian Small Cap Core VIP Fund)"
_FORMERLY = re.compile(r"\(\s*formerly[^)]*\)|,?\s*formerly\s.*$", re.I)

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]")

# Tag-ish constructs that must be dropped wholesale rather than turned into space.
_DROP_BLOCKS = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>|<!--.*?-->", re.I | re.S
)
_TAG = re.compile(r"<[^>]+>")


def unescape_stable(text: str, max_rounds: int = 4) -> str:
    """Unescape HTML entities to a fixed point.

    EDGAR SGML headers are *double* escaped: a fund named "S&P 500 Index Master
    Portfolio" appears as ``S&amp;amp;P``. A single ``html.unescape`` leaves
    ``S&amp;P``, which then normalizes to the token "amp" and silently fails to
    match the document body. Seen in MASTER INVESTMENT PORTFOLIO
    (0001193125-26-093673) and BlackRock Funds III (0001193125-26-093659).

    Iterating to a fixed point handles arbitrary nesting without assuming a
    fixed escape depth.
    """
    for _ in range(max_rounds):
        new = html.unescape(text)
        if new == text:
            return new
        text = new
    return text


def collapse_ws(text: str) -> str:
    """Collapse all whitespace runs to single spaces and strip."""
    return _WS.sub(" ", text).strip()


def textify(markup: str) -> str:
    """Flatten HTML to a single normalized text stream.

    Offsets into the returned string are the lineage anchor persisted alongside
    every extracted fact, so this function's output must be deterministic for a
    given input. Do not reorder the operations.
    """
    without_blocks = _DROP_BLOCKS.sub(" ", markup)
    stripped = _TAG.sub(" ", without_blocks)
    return collapse_ws(unescape_stable(stripped))


def fund_key(name: str) -> str:
    """Normalize a fund name into a comparison key.

    Used to match SGML header series names against names as rendered in the
    document body. Deliberately lossy: punctuation, trademark markers, former
    names, and case are all discarded.
    """
    s = unescape_stable(name)
    s = _FORMERLY.sub(" ", s)
    s = _TRADEMARK.sub(" ", s)
    return " ".join(_NON_ALNUM.sub(" ", s.lower()).split())
