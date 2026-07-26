"""Extract holdings from the schedules of investments.

Two things make this harder than a table read.

**Footnote symbols are per-filing, not standard.** One filing marks Level 3 with
``(1)`` and 144A with ``@``; another uses entirely different symbols, and the
set varies between funds *within* a single filing. So the legend is parsed from
the same section as the rows, and symbols are resolved against that local legend
rather than any global table.

**Rows are not self-contained.** A bond issuer often appears on its own row with
its tranches listed beneath, and category headings and subtotals are interleaved
with holdings. Issuer is therefore carried forward, and rows that are really
headings or subtotals are classified out rather than emitted as securities.

Security identity stays deliberately weak: N-CSR schedules frequently disclose
no CUSIP, so ``issuer`` + ``coupon`` + ``maturity_date`` is the composite key
available for tracking a position across periods.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .htmltables import ParsedDocument, Table
from .statements import normalize_value

#: Canonical flag -> phrase that identifies it in a legend entry. Matched
#: against the filing's own wording, so a symbol is only trusted when the
#: filing explains it.
LEGEND_PHRASES: Sequence[Tuple[str, "re.Pattern"]] = (
    ("level_3", re.compile(r"significant unobservable inputs|Level\s*3\s*security", re.I)),
    ("rule_144a", re.compile(r"Rule\s*144A", re.I)),
    ("non_income_producing", re.compile(r"Non-?income[- ]producing", re.I)),
    ("restricted", re.compile(r"Restricted [Ss]ecurit", re.I)),
    ("in_default", re.compile(r"in default|non-?accrual", re.I)),
    ("perpetual", re.compile(r"[Pp]erpetual security", re.I)),
    ("variable_rate", re.compile(r"[Vv]ariable rate security|[Ff]loating rate", re.I)),
    ("affiliated", re.compile(r"[Aa]ffiliated (?:company|issuer)", re.I)),
    ("illiquid", re.compile(r"[Ii]lliquid security", re.I)),
    ("pledged_collateral", re.compile(r"pledged as collateral", re.I)),
)

#: A footnote symbol: a parenthesized number, or a short run of punctuation.
_SYMBOL = r"(?:\(\d{1,2}\)|[^\w\s]{1,2})"

_COUPON = re.compile(r"(\d{1,2}\.\d{1,4})\s*%")
_MATURITY = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b")
#: Trailing footnote markers on a security description.
_TRAILING = re.compile(r"((?:\(\d{1,2}\)|[^\w\s.,%)/-])+)\s*$")
#: "Media — 4.8%" or "Healthcare Services — (continued)".
_CATEGORY = re.compile(r"^(.{2,60}?)\s*[—–-]\s*(?:\(continued\)|\(?\d[\d.,]*\)?\s*%)\s*$")
_TOTAL = re.compile(r"^\s*(?:TOTAL|Total|NET ASSETS|Other Assets)", re.I)

#: Tables that sit inside a schedule section but are not lists of securities.
#: The fair-value hierarchy summary is the important one: its rows are
#: asset-class totals ("Corporate Bonds  114,962,448"), and reading them as
#: holdings roughly doubles a fund's total. Country/sector allocation tables are
#: the same shape.
_NOT_HOLDINGS = re.compile(
    r"Level\s*[123]\b|Total Market Value|% of total investments|"
    r"Summary of inputs used to value",
    re.I,
)


#: A schedule states its own total, which makes extraction self-checking.
#: The cost parenthetical must be removed first: "TOTAL INVESTMENTS - 98.6%
#: (Cost $117,939,535) $ 121,162,999" states cost *and* market value, and for an
#: equity fund with appreciation they differ by more than twofold.
_STATED_TOTAL = re.compile(
    r"TOTAL\s+(?:INVESTMENTS?|INVESTMENTS?\s+IN\s+SECURITIES)\b.{0,140}",
    re.I | re.S,
)
_COST_PARENTHETICAL = re.compile(r"\(\s*Cost[^)]*\)", re.I)
_MONEY = re.compile(r"\$\s*([\d,]{7,})")


def stated_total(text: str) -> Optional[float]:
    """The market value a schedule reports for itself, if it states one.

    Attribution splits a schedule across many page-level sections, so this is
    applied to the union of a fund's sections rather than to one of them: the
    grand total sits at the end of the whole schedule and routinely lands in a
    different section from the holdings it totals.

    The last match wins. A schedule states subtotals on the way down ("TOTAL
    COMMON STOCKS") before the grand total, and only the last is fund-wide.
    """
    matches = list(_STATED_TOTAL.finditer(text))
    for match in reversed(matches):
        segment = _COST_PARENTHETICAL.sub("", match.group(0))
        money = _MONEY.search(segment)
        if not money:
            continue
        try:
            return float(money.group(1).replace(",", ""))
        except ValueError:
            continue
    return None


#: Scales a schedule may state its totals in. Some filings present the summary
#: in thousands or millions while listing holdings in whole dollars, and the
#: units are declared in table furniture that does not survive flattening.
_SCALES = (1.0, 1_000.0, 1_000_000.0)

#: A scale is only applied when it brings the check within this fraction.
RESOLVE_TOLERANCE = 0.01


def reconcile(holdings: Sequence[Holding], text: str):
    """Compare extracted holdings against the schedule's own stated total.

    Returns ``(extracted, stated, relative_difference)``, where ``stated`` is
    expressed in whole dollars; ``stated`` is None when the schedule reports no
    total to check against.

    The stated figure is tried at each plausible scale and the closest wins. A
    coincidental thousand-fold agreement is not credible, so this resolves the
    units rather than guessing them -- without it a correctly extracted fund
    reads as a 100,000% discrepancy.
    """
    extracted = sum(float(h.value) for h in holdings if h.value)
    stated = stated_total(text)
    if not stated:
        return extracted, None, None
    literal = abs(extracted - stated) / stated
    for scale in _SCALES[1:]:
        scaled = stated * scale
        difference = abs(extracted - scaled) / scaled
        if difference <= RESOLVE_TOLERANCE:
            return extracted, scaled, difference
    # No scale resolves the check, so report the figure as filed rather than
    # rescaling to whichever is marginally less wrong -- a misleading "stated"
    # value is worse than an honest discrepancy.
    return extracted, stated, literal


def _is_holdings_table(table: Table, document: str) -> bool:
    return not _NOT_HOLDINGS.search(document[table.start : table.end])


@dataclass(frozen=True)
class Holding:
    """One security row."""

    series_id: str
    issuer: str
    value: Optional[str] = None
    shares_or_par: Optional[str] = None
    coupon: Optional[str] = None
    maturity_date: Optional[str] = None
    category: Optional[str] = None
    flags: Tuple[str, ...] = ()
    char_start: int = 0
    char_end: int = 0


#: A standalone footnote marker: a whole whitespace-delimited token that is a
#: parenthesized number or a short run of punctuation.
_LEGEND_MARKER = re.compile(r"(?<!\S)(\(\d{1,2}\)|[^\w\s]{1,2})(?!\S)")


def parse_legend(text: str) -> Dict[str, str]:
    """Map footnote symbol -> canonical flag using the filing's own wording.

    The legend is split into entries at each standalone marker, and each entry
    is classified by the explanation that follows it. Anchoring on the marker
    rather than searching backwards from the explanation matters: "@ Security
    exempt from registration under Rule 144A of the Securities Act" puts fifty
    characters between the two.

    An entry whose text matches no known explanation is discarded, so stray
    punctuation in a schedule is never mistaken for a marker.
    """
    markers = list(_LEGEND_MARKER.finditer(text))
    legend: Dict[str, str] = {}
    for index, marker in enumerate(markers):
        stop = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        entry = text[marker.end() : stop]
        for flag, phrase in LEGEND_PHRASES:
            if phrase.search(entry):
                legend.setdefault(marker.group(1), flag)
                break
    return legend


def _resolve(symbols: str, legend: Dict[str, str]) -> Tuple[str, ...]:
    """Resolve a run of trailing markers against the local legend."""
    found: List[str] = []
    remaining = symbols.strip()
    # Longest symbols first so "††" is not read as two "†".
    for symbol in sorted(legend, key=len, reverse=True):
        while symbol and symbol in remaining:
            remaining = remaining.replace(symbol, "", 1)
            flag = legend[symbol]
            if flag not in found:
                found.append(flag)
    return tuple(found)


def _describe(description: str, legend: Dict[str, str]):
    """Split a description into issuer, coupon, maturity and flags."""
    trailing = _TRAILING.search(description)
    symbols = trailing.group(1) if trailing else ""
    body = description[: trailing.start()] if trailing else description

    # A parenthesized marker may also sit inline, e.g. "Acme Corp (1) 5.0%".
    inline = re.findall(r"\(\d{1,2}\)", body)
    flags = list(_resolve(symbols + "".join(inline), legend))

    # Some rows name the exemption in the description instead of marking it
    # with the legend symbol ("Commercial Metals Co., 144A 6.000%, 04/15/32"),
    # so the literal text counts too.
    if "144A" in description.upper() and "rule_144a" not in flags:
        flags.append("rule_144a")
    flags = tuple(flags)

    coupon = _COUPON.search(body)
    maturity = _MATURITY.search(body)

    cut = min(
        [m.start() for m in (coupon, maturity) if m] or [len(body)]
    )
    issuer = body[:cut].strip(" ,;-—–")
    if "144A" in issuer:
        issuer = issuer.replace("144A", "").strip(" ,;-—–")
    return issuer, (coupon.group(1) if coupon else None), (
        maturity.group(1) if maturity else None
    ), flags


def _value_columns(grid: Sequence[Sequence[str]]) -> Tuple[int, int]:
    """Identify the (quantity, value) columns.

    Value is the right-most column carrying numbers; quantity is the one before
    it. Schedules put the description first and money last, and the layout is
    stable within a table even when the header wording is not.
    """
    numeric_counts: Dict[int, int] = {}
    for row in grid:
        for index, cell in enumerate(row):
            if normalize_value(cell) is not None:
                numeric_counts[index] = numeric_counts.get(index, 0) + 1
    if not numeric_counts:
        return -1, -1
    # Every column carrying a number counts, however few. A short-term
    # investments table may hold a single position, and requiring more than one
    # numeric entry discarded its quantity column and with it the position.
    populated = sorted(numeric_counts)
    value_column = populated[-1]
    quantity_column = populated[-2] if len(populated) > 1 else -1
    return quantity_column, value_column


def extract_holdings(
    document: ParsedDocument,
    series_id: str,
    start: int,
    end: int,
    legend: Optional[Dict[str, str]] = None,
) -> List[Holding]:
    """Extract holdings from every schedule table in a character range."""
    section_text = document.text[start:end]
    if legend is None:
        cutoff = _NOT_HOLDINGS.search(section_text)
        legend = parse_legend(
            section_text[: cutoff.start()] if cutoff else section_text
        )

    holdings: List[Holding] = []
    for table in document.tables_within(start, end):
        if not _is_holdings_table(table, document.text):
            continue
        grid = table.grid(document.text)
        quantity_column, value_column = _value_columns(grid)
        if value_column < 0:
            continue
        # A schedule lists a quantity beside every value. A table with a single
        # numeric column is an allocation summary -- schedules close with
        # breakdowns by industry or country ("Banks 18,226,320") whose rows are
        # otherwise shaped exactly like holdings and would inflate the fund by
        # roughly its own size.
        if quantity_column < 0:
            continue

        category: Optional[str] = None
        issuer_carry: Optional[str] = None

        for row_index, row in enumerate(grid):
            description = row[0].strip() if row else ""
            value = (
                normalize_value(row[value_column])
                if value_column < len(row)
                else None
            )

            if description and value is None:
                heading = _CATEGORY.match(description)
                if heading:
                    category = heading.group(1).strip()
                    issuer_carry = None
                    continue
                if _TOTAL.match(description):
                    issuer_carry = None
                    continue
                # An issuer with its tranches listed on following rows.
                issuer_carry = description
                continue

            if value is None:
                continue
            if not description:
                continue  # category subtotal: value with no security

            if _TOTAL.match(description):
                issuer_carry = None
                continue

            issuer, coupon, maturity, flags = _describe(description, legend)
            if not issuer and issuer_carry:
                issuer, _, _, carry_flags = _describe(issuer_carry, legend)
                flags = tuple(dict.fromkeys(flags + carry_flags))
            if not issuer:
                continue

            cell = _cell(table, row_index, value_column)
            quantity = (
                normalize_value(row[quantity_column])
                if 0 <= quantity_column < len(row)
                else None
            )
            holdings.append(
                Holding(
                    series_id=series_id,
                    issuer=issuer,
                    value=value,
                    shares_or_par=quantity,
                    coupon=coupon,
                    maturity_date=maturity,
                    category=category,
                    flags=flags,
                    char_start=cell.start if cell else table.start,
                    char_end=cell.end if cell else table.end,
                )
            )
    return holdings


def _cell(table: Table, row_index: int, column: int):
    if row_index >= len(table.rows):
        return None
    position = 0
    for cell in table.rows[row_index].cells:
        if position <= column < position + cell.colspan:
            return cell
        position += cell.colspan
    return None
