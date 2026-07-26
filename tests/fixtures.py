"""Regression corpus: 15 real filings spanning 9 filing agents and 4 strata.

Documents are not vendored (they total ~250 MB). They are fetched on demand into
a gitignored cache. Every expectation below was verified by hand against the
live filing.
"""

from __future__ import annotations

import os
import time
import urllib.request
from dataclasses import dataclass
from typing import List, Optional

CACHE = os.path.join(os.path.dirname(__file__), "_cache")

# SEC requires a descriptive User-Agent with contact info, and rate-limits to
# 10 requests/second.
USER_AGENT = os.environ.get("SEC_USER_AGENT", "ncsr-pipeline research@example.com")
_REQUEST_SPACING = 0.15


@dataclass(frozen=True)
class Fixture:
    label: str
    cik: int
    accession: str
    document: str
    form_type: str
    series: int
    #: None where the stratum makes the check inapplicable.
    sections: Optional[int] = None
    opinions: Optional[int] = None
    covered: Optional[int] = None
    kind: str = "open_end_annual"
    note: str = ""

    @property
    def base(self) -> str:
        return (
            "https://www.sec.gov/Archives/edgar/data/"
            f"{self.cik}/{self.accession.replace('-', '')}"
        )

    @property
    def doc_url(self) -> str:
        return f"{self.base}/{self.document}"

    @property
    def header_url(self) -> str:
        return f"{self.base}/{self.accession}-index-headers.html"


FIXTURES: List[Fixture] = [
    # --- open-end annual, the common case -------------------------------
    Fixture("penn", 702340, "0001193125-26-092803", "d58880dncsr.htm",
            "N-CSR", 29, 1, 1, 29,
            note="fund name follows the section heading"),
    Fixture("guard", 1668512, "0001193125-26-095292", "d102894dncsr.htm",
            "N-CSR", 24, 24, 24, 24,
            note="24 concatenated Item 1-11 blocks, one per fund"),
    Fixture("gugg", 217087, "0001398344-26-004846", "fp0097842-6_ncsrixbrl.htm",
            "N-CSR", 3, 1, 1, 3,
            note="embedded contents list with dot leaders truncates the span"),
    Fixture("imst", 1587982, "0001398344-26-004870", "fp0097170-1_ncsrixbrl.htm",
            "N-CSR", 3, 1, 1, 3,
            note="fund name precedes the section heading"),
    Fixture("templeton", 865722, "0001133228-26-002516", "tif-efp22445_ncsr.htm",
            "N-CSR", 2, 1, 1, 2,
            note="ALL CAPS headings; source typo 'FINANCIAL HIGLIGHTS'"),
    Fixture("blackrock", 319108, "0000319108-26-000003", "primary-document.htm",
            "N-CSR", 5, 1, 2, 5,
            note="en-dash item separator"),
    Fixture("victory", 802716, "0000802716-26-000007", "primary-document.htm",
            "N-CSR", 15, 1, 4, 15,
            note="4 opinions; 'Schedule of Portfolio Investments' only"),
    Fixture("nlfund", 1644419, "0001580642-26-000089", "mainetfs_ncsr.htm",
            "N-CSR", 3, 1, 1, 3),
    Fixture("voya", 1090682, "0001104659-26-025188", "tm263417d3_ncsr.htm",
            "N-CSR", 1, 1, 2, 1,
            note="VY(R) vs VY (R) trademark normalization"),
    # --- strata ----------------------------------------------------------
    Fixture("semiannual", 1370177, "0001398344-26-010423",
            "fp0098626-1_ncsrsixbrl.htm", "N-CSRS", 2, 1, None, None,
            kind="open_end_semiannual",
            note="unaudited: no opinion exists, coverage not applicable"),
    Fixture("consolidated", 1557794, "0001193125-26-259907", "d155897dncsr.htm",
            "N-CSR", 1, 1, 1, 1,
            note="'Consolidated Schedule of Investments'"),
    Fixture("master", 915092, "0001193125-26-093673", "d100815dncsr.htm",
            "N-CSR", 8, 1, 13, 8,
            note="master side; header double-escapes 'S&amp;amp;P'"),
    Fixture("feeder", 893818, "0001193125-26-093659", "d101052dncsr.htm",
            "N-CSR", 7, 1, 13, 7,
            note="feeder side; holdings overlap master -> analytics must dedupe"),
    Fixture("closedend", 1478888, "0001193125-26-258824", "d106843dncsr.htm",
            "N-CSR", 0, None, None, None, kind="closed_end",
            note="no series roster; financials in Item 1"),
    Fixture("closedend2", 1832871, "0001193125-26-093583", "d222609dncsr.htm",
            "N-CSR", 0, None, None, None, kind="closed_end",
            note="no series roster; en-dash headings"),
]

BY_LABEL = {f.label: f for f in FIXTURES}


def _download(url: str, path: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = response.read()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)
    time.sleep(_REQUEST_SPACING)


def load(fixture: Fixture) -> "tuple[str, str]":
    """Return ``(document_markup, header_markup)``, fetching and caching once."""
    doc_path = os.path.join(CACHE, f"{fixture.label}.htm")
    hdr_path = os.path.join(CACHE, f"{fixture.label}.hdr")

    if not os.path.exists(doc_path):
        _download(fixture.doc_url, doc_path)
    if not os.path.exists(hdr_path):
        _download(fixture.header_url, hdr_path)

    with open(doc_path, encoding="utf-8", errors="replace") as handle:
        document = handle.read()
    with open(hdr_path, encoding="utf-8", errors="replace") as handle:
        header = handle.read()
    return document, header
