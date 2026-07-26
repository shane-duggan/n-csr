"""Model-backed review stages.

Two of the questions this pipeline exists to answer cannot be settled by
parsing:

*Which funds have accruals or payables related to indemnifications,
contingencies or litigation?* Nearly every filing contains indemnification
language, and nearly all of it is boilerplate -- "the risk of loss to be
remote". A keyword search answers "almost all of them", which is useless. The
judgement is boilerplate versus a real, quantified exposure.

*Does the audit opinion meet PCAOB standards and include the required
elements?* Coverage of every series and the presence of the required dates are
already checked deterministically; what remains is whether the opinion actually
says what an opinion must say.

Both stages hand the model a short, retrieved excerpt and take back a verdict
with a rationale, which is stored beside the excerpt so a reviewer reads the
filing's own words rather than a paraphrase.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .candidates import Passage, looks_like_boilerplate, retrieve
from .llm import Judge, Verdict, parse_verdicts
from .records import FindingRecord, Provenance

CONTINGENCY_SYSTEM = """\
You are reviewing excerpts from the notes to a registered investment company's \
financial statements, filed on SEC Form N-CSR.

For each excerpt, decide whether it discloses a real exposure or is standard \
boilerplate.

Classify each as exactly one of:

- "substantive": discloses an actual or reasonably possible loss -- a named \
proceeding, a quantified accrual or payable, a recorded liability, a \
regulatory action, or a settlement. Anything a reviewer would need to follow up.
- "boilerplate": the routine indemnification and contractual-obligation \
language nearly every fund files. Typically states that maximum exposure is \
unknown because it depends on future claims, and that the risk of loss is \
considered remote. No specific matter is identified.
- "unrelated": the trigger word appears in another sense entirely -- trade \
settlement, expense-cap definitions, asset classes such as "trade claims".

Judge only what the excerpt says. Do not infer a proceeding from the mere \
presence of indemnification language, and do not treat an absence of detail as \
evidence of a hidden exposure. Most excerpts are boilerplate; say so plainly \
when they are.

Give a confidence between 0 and 1, and a one-sentence rationale quoting the \
words that decided it."""

VERDICT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "verdict": {
                        "type": "string",
                        "enum": ["substantive", "boilerplate", "unrelated"],
                    },
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                },
                "required": ["index", "verdict", "confidence", "rationale"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


def build_prompt(passages: Sequence[Passage]) -> str:
    """Render passages for adjudication, numbered so verdicts can be matched."""
    blocks = []
    for index, passage in enumerate(passages):
        blocks.append(
            f"<excerpt index=\"{index}\" topic=\"{passage.topic}\">\n"
            f"{passage.text}\n"
            f"</excerpt>"
        )
    return "\n\n".join(blocks)


def _provenance(analysis, passage: Passage, judge: Judge, confidence: float) -> Provenance:
    header = analysis.header
    return Provenance(
        accession=header.accession or "",
        cik=header.cik or "",
        period=header.period or "",
        pipeline_version=analysis.pipeline_version,
        audited=analysis.audited,
        series_id=passage.series_id or None,
        section_type=passage.section_type,
        char_start=passage.start,
        char_end=passage.end,
        method="llm",
        confidence=confidence,
        model_id=judge.model_id,
    )


def triage_contingencies(
    analysis,
    text: str,
    judge: Judge,
    passages: Optional[Sequence[Passage]] = None,
) -> List[FindingRecord]:
    """Separate real contingency exposure from routine indemnification language.

    Returns a finding per adjudicated passage. A ``substantive`` verdict is an
    exception; boilerplate is recorded as ``info`` rather than discarded, so a
    reviewer can see that the language was found and judged rather than missed.
    """
    if passages is None:
        passages = retrieve(text, analysis.sections)
    if not passages:
        return []

    raw = judge.adjudicate(
        CONTINGENCY_SYSTEM, build_prompt(passages), VERDICT_SCHEMA
    )
    verdicts = parse_verdicts(raw, expected=len(passages))

    findings: List[FindingRecord] = []
    for verdict in verdicts:
        passage = passages[verdict.index]
        substantive = verdict.is_substantive
        findings.append(
            FindingRecord(
                provenance=_provenance(analysis, passage, judge, verdict.confidence),
                finding_type="contingency_exposure",
                severity="exception" if substantive else "info",
                summary=verdict.rationale
                or f"{passage.topic}: {verdict.verdict}",
                excerpt=passage.text,
                passed=not substantive,
            )
        )
    return findings


def unreviewed_passages(
    passages: Sequence[Passage], findings: Sequence[FindingRecord]
) -> List[Passage]:
    """Passages the model returned no verdict for.

    A dropped or malformed verdict must not look like a clean result, so these
    are surfaced for review rather than assumed benign.
    """
    reviewed = {(f.provenance.char_start, f.provenance.char_end) for f in findings}
    return [p for p in passages if (p.start, p.end) not in reviewed]


def prefilter_savings(passages: Sequence[Passage]) -> Dict[str, int]:
    """How much of the batch the deterministic hint already explains.

    Boilerplate is still sent -- the model, not a regex, decides -- but knowing
    the share makes the cost of the stage predictable.
    """
    likely = sum(1 for p in passages if looks_like_boilerplate(p))
    return {
        "passages": len(passages),
        "likely_boilerplate": likely,
        "characters": sum(p.length for p in passages),
    }
