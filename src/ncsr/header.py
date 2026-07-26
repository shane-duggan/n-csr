"""EDGAR SGML header parsing.

The header is the authoritative fund roster for a filing: it carries the
``SERIES-ID`` / ``SERIES-NAME`` pairs that key everything downstream. It is also
the answer key used to select a body-parsing strategy (see ``sectioner``) and to
verify audit-opinion coverage (see ``audit``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Optional

from .normalize import unescape_stable

_SERIES = re.compile(r"<SERIES-ID>(S\d+)\s*<SERIES-NAME>([^\n<]+)")

# Forms whose financial statements are audited. N-CSRS is the semi-annual
# report: same Item 7 structure, but unaudited and carrying no audit opinion.
AUDITED_FORMS = frozenset({"N-CSR", "N-CSR/A"})
SEMIANNUAL_FORMS = frozenset({"N-CSRS", "N-CSRS/A"})


def _field(tag: str, text: str) -> Optional[str]:
    m = re.search(re.escape(tag) + r":\s*(.+)", text)
    return m.group(1).strip() if m else None


@dataclass
class Header:
    """Parsed EDGAR submission header."""

    accession: Optional[str] = None
    form_type: Optional[str] = None
    cik: Optional[str] = None
    registrant: Optional[str] = None
    period: Optional[str] = None
    #: series_id -> series_name. Deduplicated by ID.
    series: Dict[str, str] = field(default_factory=dict)

    @property
    def has_series(self) -> bool:
        """False for closed-end registrants, where the registrant *is* the fund.

        Verified on Nuveen Taxable Municipal Income Fund (0001193125-26-258824)
        and BlackRock 2037 Municipal Target Term Trust (0001193125-26-093583):
        neither carries a SERIES-AND-CLASSES-CONTRACTS-DATA block at all.
        """
        return bool(self.series)

    @property
    def is_audited(self) -> bool:
        return (self.form_type or "").upper() in AUDITED_FORMS

    @property
    def is_semiannual(self) -> bool:
        return (self.form_type or "").upper() in SEMIANNUAL_FORMS


def parse_header(markup: str) -> Header:
    """Parse an EDGAR ``*-index-headers.html`` document (or a raw .hdr.sgml).

    The header block is rendered twice in ``index-headers.html``, so series are
    deduplicated by ``SERIES-ID`` -- Victory Portfolios (0000802716-26-000007)
    shows 30 SERIES-ID tags for 15 real series.
    """
    text = unescape_stable(markup)
    series: Dict[str, str] = {}
    for sid, name in _SERIES.findall(text):
        series.setdefault(sid, name.strip())

    return Header(
        accession=_field("ACCESSION NUMBER", text),
        form_type=_field("CONFORMED SUBMISSION TYPE", text),
        cik=_field("CENTRAL INDEX KEY", text),
        registrant=_field("COMPANY CONFORMED NAME", text),
        period=_field("CONFORMED PERIOD OF REPORT", text),
        series=series,
    )
