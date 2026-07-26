"""Model-backed judgement, kept behind a seam.

Only a few questions in an N-CSR review genuinely need a model. Everything that
can be decided by parsing already is, so what reaches this module is a short
list of passages a rule cannot adjudicate -- roughly 600 tokens per filing.

The seam matters as much as the model. ``Judge`` is a protocol, so candidate
retrieval, prompt construction and the mapping from a verdict to a
``FindingRecord`` are all exercised offline; ``ScriptedJudge`` makes the review
stages testable without credentials or network. ``AnthropicJudge`` imports the
SDK lazily, so the package installs and runs without it.

Every model verdict lands in the same ``findings`` table as the rule-derived
ones, distinguished by ``method='llm'`` plus a ``model_id`` and a confidence, so
a reviewer can always filter by how a conclusion was reached.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

#: Default model. Opus is the right tier here: these are judgement calls a
#: reviewer may be held to, and the volume is tiny because retrieval is narrow.
DEFAULT_MODEL = "claude-opus-5"


@dataclass(frozen=True)
class Verdict:
    """One adjudicated passage."""

    index: int
    verdict: str
    confidence: float
    rationale: str

    @property
    def is_substantive(self) -> bool:
        return self.verdict == "substantive"


class Judge:
    """Protocol for anything that can adjudicate a batch of passages."""

    model_id = "none"

    def adjudicate(
        self, system: str, prompt: str, schema: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError


class ScriptedJudge(Judge):
    """Test double returning pre-set verdicts, and recording what it was asked.

    Lets the review stages be tested end to end -- retrieval, prompt, parsing,
    finding construction -- with no network and no credentials.
    """

    model_id = "scripted"

    def __init__(self, responses: Optional[Sequence[Dict[str, Any]]] = None):
        self.responses = list(responses or [])
        self.calls: List[Dict[str, str]] = []

    def adjudicate(self, system, prompt, schema):
        self.calls.append({"system": system, "prompt": prompt})
        return self.responses


class AnthropicJudge(Judge):
    """Real implementation against the Claude API."""

    def __init__(self, client: Any = None, model: str = DEFAULT_MODEL):
        self._client = client
        self.model_id = model

    def _ensure_client(self):
        if self._client is None:
            import anthropic  # imported lazily: optional dependency

            self._client = anthropic.Anthropic()
        return self._client

    def adjudicate(self, system, prompt, schema):
        client = self._ensure_client()
        response = client.messages.create(
            model=self.model_id,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            # The rubric is identical across every filing, so caching it turns
            # the per-filing cost into just the passages.
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            return []
        text = next(
            (b.text for b in response.content if getattr(b, "type", None) == "text"), ""
        )
        if not text:
            return []
        payload = json.loads(text)
        return payload.get("verdicts", [])


def parse_verdicts(raw: Sequence[Dict[str, Any]], expected: int) -> List[Verdict]:
    """Convert a model response into verdicts, discarding anything malformed.

    A verdict naming a passage that was not sent is dropped rather than
    reindexed: silently reassigning it would attach a rationale to the wrong
    excerpt, which is worse than losing the judgement.
    """
    verdicts: List[Verdict] = []
    for entry in raw:
        try:
            index = int(entry["index"])
            confidence = float(entry.get("confidence", 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= index < expected:
            continue
        verdict = str(entry.get("verdict", "")).strip().lower()
        if verdict not in {"substantive", "boilerplate", "unrelated"}:
            continue
        verdicts.append(
            Verdict(
                index=index,
                verdict=verdict,
                confidence=min(max(confidence, 0.0), 1.0),
                rationale=str(entry.get("rationale", "")).strip()[:600],
            )
        )
    return verdicts
