# N-CSR Item 7 extraction pipeline

Extracts and reviews the financial statements and schedules in **Item 7** of SEC
Form N-CSR, at fund (series) granularity, for AWS Lambda on Python 3.12.

Design goal is precision at low cost: deterministic parsing wherever the filing
structure permits it, an LLM only where the task is genuinely a judgment call,
and per-fact lineage so a reviewer can always see where a number came from.

## Status

Built and validated: **sectioning, classification, audit-coverage
reconciliation, per-fund attribution, the storage write path, and extraction of
statement line items and holdings**. The LLM review stages are not yet
implemented.

Scope is currently annual open-end N-CSR. N-CSRS is classified and carries
`audited=false` but is not a focus; N-CSR/A and closed-end funds are out of
scope by decision.

```
python3 -m pytest -q                          # 182 tests
python3 -m ncsr.cli DOC.htm HEADER.hdr        # manifest as JSON
python3 -m ncsr.cli DOC.htm HEADER.hdr --emit ./out
python3 -m ncsr.cli --ddl s3://bucket/ncsr    # Iceberg DDL
```

The test suite runs against 15 real filings. The first run downloads ~250 MB
into `tests/_cache/` (gitignored); set `SEC_USER_AGENT` to your own contact
string, as the SEC requires one.

## What's built

| Module | Responsibility |
|---|---|
| `normalize` | HTML → normalized text; fund-name comparison keys |
| `header` | EDGAR SGML header → series roster, form type, period |
| `sectioner` | Locate Item 7 spans in the text stream |
| `audit` | Extract audit opinions; reconcile against the series roster |
| `attribution` | Split Item 7 into per-fund sections |
| `master_feeder` | Detect feeder→master relationships; look-through policy |
| `pipeline` | Classify the filing; produce the manifest |
| `records` | Row shapes and lineage for the analytical tables |
| `ddl` | Athena/Iceberg table definitions |
| `store` | Storage boundary; `LocalStore` reference implementation |
| `htmltables` | Offset-preserving HTML parse; table and cell geometry |
| `statements` | Line items from the financial statements |
| `holdings` | Schedule-of-investments rows, legend resolution, reconciliation |
| `fairvalue` | Fair-value hierarchy table; Level 1/2/3 per asset class |
| `emit` | Persist evidence, rows, and the commit marker |

`analyze()` returns a `FilingAnalysis` whose `manifest()` is the payload written
to DynamoDB **last**, as the commit marker that makes reprocessing idempotent.
Bumping `PIPELINE_VERSION` invalidates every stored manifest and forces a
backfill without a delete step.

## Validation

15 filings, 9 filing agents, 4 structural strata:

- **101/101** series reconciled to an audit opinion across 13 open-end N-CSRs.
- **100/101** series located with a holdings schedule. The one absence is
  genuine: BlackRock Cash Funds: Treasury is a feeder holding master shares.
- **93.4%** of fund-specific content attributed to a named fund corpus-wide;
  9 of 12 filings clear the 85% review threshold, the rest are flagged.
- Section counts exact on every filing, including Guardian VP Trust's 24
  concatenated per-fund reports.
- **3,559 statement line items** extracted across 9 filings. Penn Series'
  Money Market Fund dividend income comes out at **822,559**, matching the
  filing, with Interest and Total Investment Income also exact.
- **42,079 holdings** extracted, and **62 funds** reconciled against the total
  their own schedule states -- 17 exactly, 39 within 1% (63%). Penn's High Yield
  Bond Fund reconciles to its stated 121,162,999 and, independently, its Rule
  144A holdings sum to the stated 99,518,726 -- 81.0% of net assets, matching
  exactly.
- **1,332 fair-value rows** across 36 funds, every table self-consistent: on
  each row the levels sum to the stated total.
- 201 MB analyzed in 1.31 s (153 MB/s), peak RSS 292 MB; the table parse adds
  15 s for the same corpus (13 MB/s, largest filing 3.1 s).

Attribution quality per filing, with anything below 85% routed to review:

| | coverage | | coverage |
|---|---:|---|---:|
| voya, consolidated | 100% | imst | 94.1% |
| guard | 98.6% | templeton | 89.3% |
| penn | 98.5% | feeder | 88.6% |
| master | 95.5% | blackrock | 82.9% ⚠ |
| nlfund | 94.2% | gugg | 62.9% ⚠ |
| | | victory | 35.5% ⚠ |

Performance means the sectioning stage is nowhere near the Lambda 15-minute
limit; a survey of 290 filings put the size distribution at p50 1.3 MB, p90
9.7 MB, p99 27 MB, max 46 MB, with nothing above 50 MB.

## Findings that shaped the code

Each of these broke a plausible-looking implementation. They are encoded as
comments at the relevant rule and as fixtures in the regression corpus.

**Layout does not track the filing agent.** Penn Series and Guardian VP Trust
share agent `0001193125` and use entirely different structures. Parsing strategy
must be detected per filing, not looked up by filer.

**Item 7 headings vary more than the form suggests.** Templeton writes them in
ALL CAPS and contains the source typo `FINANCIAL HIGLIGHTS`; BlackRock separates
with an en-dash; Guggenheim embeds a contents list that truncates naive spans.
The matcher tolerates case, five separator characters, and a truncated keyword
stem, and rejects headings followed by dot leaders.

**One filing is not one report.** Guardian concatenates 24 complete Item 1-11
blocks; Victory concatenates four annual reports with four separate audit
opinions. Audit coverage is a union across all opinions -- taking only the first
reported 5/15 for Victory.

**Invert the opinion check.** Parsing an opinion's own fund list is brittle
(enumerated after "comprised of", in the addressee line, or in a trailing
table). Testing each *known* series name for presence in the opinion instead is
what got coverage to 101/101.

**EDGAR headers are double HTML-escaped.** `S&P 500 Index Master Portfolio`
arrives as `S&amp;amp;P`. Any fund with an ampersand silently failed to
reconcile until unescaping ran to a fixed point.

**Section names are not standard.** Victory uses *Schedule of Portfolio
Investments* exclusively -- zero occurrences of the common spelling. Voya uses
*Portfolio of Investments*; Blackstone uses the Consolidated form.

**Semi-annual reports are unaudited.** N-CSRS filings (≈1,400 per half-year,
comparable to N-CSR volume) carry no audit opinion at all. Running the coverage
check on them reports a spurious 0/N gap, so it is skipped and every fact is
stamped `audited=false`.

**Closed-end funds are a different shape entirely.** They carry no series roster
(the registrant *is* the fund) and their financials live in Item 1, not Item 7.
They are classified and recorded with a `skip_reason`, never silently dropped.

## Master-feeder policy: look-through

A feeder fund invests all of its assets in a master portfolio, and the master's
holdings are reported in *both* filings. BlackRock goes further and files a
near-identical document body under two CIKs — MASTER INVESTMENT PORTFOLIO
(8 master series) and BlackRock Funds III (7 feeder series) differ by less than
1% of their text. A naive cross-fund aggregate counts those securities twice.

**The feeder is credited.** It is the registered fund under review, so it
carries the position; master series are listed in `aggregate_excluded_series`
and stay queryable by opting in. Nothing is discarded — the master's schedule is
evidence, and some masters may not file separately.

Detection anchors on the master-side declaration and then scans backwards for a
known series name from the filing's own roster, the same answer-key approach
used for attribution — the leading clause varies too much to parse directly:

> … iShares S&P 500 Index Fund (the "Fund") … **invests all of its assets in the
> S&P 500 Index Master Portfolio** (the "Master Portfolio") …

All 7 feeders resolve to their masters, all 8 masters are flagged, and there are
no false positives across the other 13 filings. This also explains the one
series with no holdings schedule: BlackRock Cash Funds: Treasury holds only
master shares, so its absence is expected and no longer counts as a parsing
miss.

Scale check: "Master Portfolio" appears in roughly 30 of 1,175 N-CSRs in a
six-month window — about 2–3% of filings.

## Table extraction

Sectioning works on flattened text, but table extraction needs cell structure,
which flattening destroys. `htmltables` rebuilds the *same* text while streaming
the markup, recording where each table and cell lands -- so offsets agree by
construction rather than by re-matching. The invariant `parse(markup).text ==
textify(markup)` is asserted against all 15 filings; if it ever breaks, every
stored offset silently points at the wrong text.

Stdlib `html.parser` is used deliberately: no native dependency to package for
Lambda, and streaming gives exact control over how the text is assembled.

Fund identity comes from the *column*, not the row. Penn reports four funds side
by side in one Statement of Operations, so the column map is read from the
table's own header row. Filings that give each fund its own section (Guardian)
name it in a banner instead, and fall back to section attribution -- mapping
only the first numeric column, because a Statement of Changes in Net Assets puts
the prior period in the second and attributing both to one fund would silently
double its figures.

## Holdings extraction

A schedule states its own total, which makes extraction self-checking:
`reconcile()` compares extracted holdings against it and emits a
`holdings_reconciliation` finding, an exception when they disagree by more than
1%. Extraction that silently disagrees with the filing is worse than extraction
that admits it.

Two things make this harder than a table read.

**Footnote symbols are local, not standard.** One filing marks Level 3 with
`(1)` and 144A with `@`; the set varies between funds *within* one filing. Worse,
the same symbol is reused with different meanings in one section — Penn's `(1)`
means "Level 3 security" in the row legend and "internally fair valued at zero"
in the valuation-hierarchy footnote below it. The legend is therefore parsed
only from the row-legend region, split into entries at each standalone marker
and classified by the filing's own wording.

**Not every table in a schedule section is a list of securities.** The
fair-value hierarchy summary sits inside the same section and its rows are
asset-class totals (`Corporate Bonds 114,962,448`); reading them as holdings
roughly doubled the fund's total before they were excluded.

Reading the stated total requires care of its own. `TOTAL INVESTMENTS — 98.6%
(Cost $117,939,535) $ 121,162,999` gives cost *and* market value, and for an
equity fund with appreciation they differ by more than twofold. A schedule also
states subtotals on the way down, so the last grand total wins.

Reconciliation is per **fund**, not per section: attribution splits a schedule
across page-level sections and the grand total lands in whichever one ends it.

Three things had to be right before the check was worth trusting, each found by
a fund that failed it:

- **Allocation summaries are not holdings.** A schedule closes with breakdowns
  by industry or country (`Banks 18,226,320`) whose rows are shaped exactly like
  securities. Counting them inflated a fund by roughly its own size.
- **A single-position table still has a quantity column.** Requiring a column to
  carry more than one number discarded the quantity column of a one-line
  short-term investments table, and with it the position.
- **Units are declared in table furniture that flattening destroys.** Some
  filings state the summary in thousands while listing holdings in dollars, so
  a correct extraction read as a 100,000% discrepancy. A scale is applied only
  when it brings the check into agreement, never to make a failure look
  smaller.

## Level 3 exposure

`fair_value_levels` is the authoritative source, not the per-security footnote
flags. Funds disclose Level 1/2/3 amounts per asset class directly, so the
figure comes from the filing's own arithmetic rather than from resolving
filing-local symbols. `ddl.LEVEL_3_BY_FUND` is the reference query.

The table is self-checking — levels must sum to the stated total on every row —
and a failure raises a `fair_value_hierarchy_inconsistent` exception rather than
being silently believed. Every table in the corpus currently reconciles.

Two distinctions the extractor preserves:

- **A dash is a disclosed zero.** `Level 3: —` is the fund affirming it holds
  nothing at Level 3, which is a different fact from having said nothing. 33 of
  36 funds affirm zero; Penn's Large Growth Stock Fund holds 579,833 in Level 3
  preferred stocks.
- **A grand total is not a subtotal.** A hierarchy table may carry several rows
  beginning "Total"; taking the first reported an asset-class subtotal as the
  fund-wide figure.

## Known limitations

- **Victory Portfolios splits its holdings across the Item 1/Item 7 boundary.**
  86 of its 145 "Schedule of Portfolio Investments" occurrences sit *outside*
  the Item 7 spans, starting at offset 253,788 while Item 7 opens at 552,393 --
  so its Item 7 is 78% notes and only 4% holdings. Report splitting (below) did
  not move its coverage, because the content is not in the span at all. This is
  a span-boundary question specific to that filer and needs a targeted look, not
  more general heuristics. Flagged `needs_review`.
- **Guggenheim (62.9%)** leaves large stretches unattributed within a single
  report. Not yet diagnosed. Flagged `needs_review`.
- **Four filings lay financial data out in `<div>`, not `<table>`.** BlackRock
  uses 92,357 `<div>` against 1,750 `<td>`; Guggenheim, Templeton and Victory
  are the same shape. Statement extraction declines on them rather than
  inventing rows, so they yield zero line items. Notably these four are also 4
  of the 5 worst attribution scores, which suggests a shared root cause worth
  investigating before building a second extraction strategy.
- **23 of 62 reconciled funds still disagree**, in three groups: a stated total
  that is really a minor trailing total (Guardian, ratio ~1000x), over-extraction
  (Blackstone, RiverNorth, ratio 2-3.5x), and under-extraction where few
  holdings are found at all (Kennedy/IMST, Main ETFs). Each is reported as a
  `holdings_reconciliation` exception rather than hidden.
- **Guardian yields only 29 line items across 23 funds.** Its per-fund sections
  are found and mapped, but few captions come through. Under-diagnosed.
- **Comparative prior-period columns are not captured** for single-fund
  statements -- see *Table extraction*.
- **Master-feeder look-through is detected but not yet enforced.** The
  relationship is extracted (see below) and masters are marked
  `aggregate_excluded_series`, but nothing consumes that flag until the fact
  tables exist. Cross-fund aggregates must apply it or they will double count.
- **Closed-end funds** are out of v1 scope.
- **Pre-July-2024 filings** are untested. Before the tailored shareholder report
  rule, financials sat in Item 1 under different item numbering, which is a
  separate extraction path.
- **`N-CSR/A` amendments** are classified but supersede-vs-retain semantics are
  not yet defined.

## Roadmap

1. ~~Sectioning, classification, audit reconciliation~~ ✅
2. ~~Per-fund attribution within Item 7, with low-confidence filings routed to
   review~~ ✅
3. ~~Split multi-report Item 7 spans at report boundaries~~ ✅ (corpus
   attribution 91% -> 93.4%; master and feeder cleared the review threshold)
4. ~~Iceberg table definitions and the manifest-commit write path~~ ✅
5. ~~Offset-preserving HTML parsing; populate `statement_lines`~~ ✅
6. ~~Holdings extraction: rows, legend resolution, reconciliation~~ ✅
7. ~~Fair-value hierarchy table as the primary source of Level 3 amounts~~ ✅
8. ~~Broaden reconciliation coverage~~ ✅ (33 sections -> 62 funds, 63% agreeing)
9. A `<div>`-layout extraction strategy (4 of 15 filings).
10. LLM stages: legend normalization, contingency triage, PCAOB judgment.

## Storage model

Three destinations, deliberately separate.

**Archival tree** — immutable evidence, browsable by fund, never queried by
Athena. Content is written once: a columnar statement covering four funds lands
under `series=_shared` rather than being duplicated under each, and the section
row carries the full `series_ids` list so a UI can still group by fund.

```
filings/cik=…/accession=…/series=…/section=…/{span:03d}-{offset:08d}.txt
```

**Analytical Iceberg tables** — `sections`, `findings`, `holdings`,
`statement_lines`, partitioned by `fiscal_period`. Iceberg rather than raw
Parquet because reprocessing is a certainty: superseding one accession is a
`DELETE … WHERE accession = …` rather than a partition rewrite with a window of
partial data. Writing analytics-ready Parquet into the archival tree instead
would produce on the order of 700k tiny files a year.

**Manifest** — the commit marker, written **last**. Rows are idempotent on
`(accession, pipeline_version)`, so a crash mid-write leaves orphaned rows that
the next run supersedes; only the manifest marks a filing complete. This
replaces a transaction, which would not fit — DynamoDB caps `TransactWriteItems`
at 100 items and Guardian VP Trust alone emits 248 section rows.

DynamoDB is the control plane only: manifests, idempotency, review queue, point
lookups. It cannot serve the analytical queries — no aggregation, no joins, no
full-text — so those belong in Athena.

### Three invariants on every row

| | |
|---|---|
| `audited` | From the form type. Audited and unaudited figures must never be compared silently. |
| `aggregate_eligible` | False for master portfolios. Default aggregates filter on it; opting in is explicit. |
| `pipeline_version` | Lets a reprocess supersede prior rows without a delete step. |

Lineage is a character range into the archived text, verified by test to
round-trip. Findings carry `method` (`regex` today, `llm` later) and
`confidence`, so a reviewer can filter by how a conclusion was reached.
