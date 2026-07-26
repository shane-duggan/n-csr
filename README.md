# N-CSR Item 7 extraction pipeline

Extracts and reviews the financial statements and schedules in **Item 7** of SEC
Form N-CSR, at fund (series) granularity, for AWS Lambda on Python 3.12.

Design goal is precision at low cost: deterministic parsing wherever the filing
structure permits it, an LLM only where the task is genuinely a judgment call,
and per-fact lineage so a reviewer can always see where a number came from.

## Status

Milestones 1 and 2 are built and validated: **sectioning, classification,
audit-coverage reconciliation, and per-fund attribution**. Fact extraction,
storage, and the LLM review stages are not yet implemented -- see *Roadmap*.

Scope is currently annual open-end N-CSR. N-CSRS is classified and carries
`audited=false` but is not a focus; N-CSR/A and closed-end funds are out of
scope by decision.

```
python3 -m pytest -q          # 107 tests
python3 -m ncsr.cli DOC.htm HEADER.hdr
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
| `pipeline` | Classify the filing; emit the manifest |

`analyze()` returns a `FilingAnalysis` whose `manifest()` is the payload written
to DynamoDB **last**, as the commit marker that makes reprocessing idempotent.
Bumping `PIPELINE_VERSION` invalidates every stored manifest and forces a
backfill without a delete step.

## Validation

15 filings, 9 filing agents, 4 structural strata:

- **101/101** series reconciled to an audit opinion across 13 open-end N-CSRs.
- **100/101** series located with a holdings schedule. The one absence is
  genuine: BlackRock Cash Funds: Treasury is a feeder holding master shares.
- **91%** of fund-specific content attributed to a named fund corpus-wide;
  8 of 12 filings clear the 85% review threshold, the rest are flagged.
- Section counts exact on every filing, including Guardian VP Trust's 24
  concatenated per-fund reports.
- 201 MB analyzed in 1.31 s (153 MB/s), peak RSS 292 MB.

Attribution quality per filing, with anything below 85% routed to review:

| | coverage | | coverage |
|---|---:|---|---:|
| voya, consolidated | 100% | templeton | 89% |
| guard | 98.6% | master | 87.8% |
| penn | 98.5% | feeder | 84.1% ⚠ |
| nlfund | 94.2% | blackrock | 82.9% ⚠ |
| imst | 94.1% | gugg | 62.9% ⚠ |
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

## Known limitations

- **Multi-report Item 7 spans attribute poorly.** Victory Portfolios (35.5%)
  concatenates four complete annual reports inside a *single* Item 7 span, each
  with its own contents page, audit opinion, and back matter. Because a section
  runs from its heading to the next one, its opinion sections run 20-34k chars
  straight through a report boundary into the next report's front matter. The
  fix is to split such spans at report boundaries before attributing, rather
  than to loosen heading detection. Guggenheim (62.9%) is a milder case of the
  same shape. Both are flagged `needs_review`, so nothing silently flows
  downstream.
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
3. Split multi-report Item 7 spans at report boundaries (unblocks Victory and
   Guggenheim; see *Known limitations*).
4. Iceberg table definitions (`holdings`, `statement_lines`, `findings`) and the
   manifest-commit write path.
5. Table-level extraction with offset-preserving HTML parsing.
6. LLM stages: legend normalization, contingency triage, PCAOB judgment.

## Storage model (target)

Archival tree, one directory per fund-section, immutable, never queried
directly:

```
s3://…/filings/cik=…/accession=…/series=…/section=…/{raw.html,text.txt,lineage.json}
```

Analytical Iceberg tables, compacted and partitioned by fiscal period, holding
the queryable facts. Writing analytics-ready Parquet into the archival tree
would produce ~700k tiny files per year and make Athena both slow and expensive.

DynamoDB is the control plane only: run manifests, idempotency, review queue,
and point lookups. It cannot serve the analytical queries -- no aggregation, no
joins, no full-text -- so those belong in Athena.
