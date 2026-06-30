"""Step 4 backend: the LLM decision step.

The ONLY place a model is required. Given the current procedure text and the
analyzed workload, the model proposes a single smallest-safe change as strict
JSON. Two interchangeable backends are provided: Gemini (Vertex AI) and Claude.

The prompt deliberately constrains the model to ONE change per iteration with a
rollback, so the loop stays auditable and reversible.
"""
from __future__ import annotations

import json
from typing import Optional, Protocol

from .models import Change, PlanScore


SYSTEM_PROMPT = """You are a senior SQL Server performance engineer.
You are given a stored procedure and a deterministic analysis of its execution
plans across a representative set of parameter values. Propose exactly ONE
smallest, safest change that will improve the MAJORITY of the parameter calls
without regressing others.

Prefer changes in this order:
  1. OPTION (RECOMPILE) or OPTIMIZE FOR hints to address parameter sniffing
  2. A targeted nonclustered index (only if impact is high and justified)
  3. A SARGability rewrite (remove function-wrapped predicates, fix implicit conversions)
  4. Query/structure rewrite as a last resort

When you propose a nonclustered index (kind="index"), follow SQL Server
indexing best practices — an extra index is a permanent write tax, so the bar
is high:
  - Only propose it when the analysis justifies it (a missing-index signal, a
    key lookup, or a scan over a large input). Never add an index speculatively.
  - Order key columns: equality predicates before range predicates, and within
    the equality columns put the most selective column first.
  - Use INCLUDE only for columns needed in the SELECT list to make the index
    covering; do not bloat it — every included column adds write and storage cost.
  - Do not create an index that overlaps an existing one (e.g. a left-prefix
    subset); prefer extending or INCLUDE-ing on the existing index instead.
  - State the write-cost / storage impact in the rationale.
  - If stale statistics or a hint would fix the plan more cheaply than a new
    index, prefer that.

Return STRICT JSON only, no markdown, with this exact shape:
{
  "kind": "option_hint|index|rewrite|recompile|none",
  "rationale": "one paragraph",
  "apply_sql": "T-SQL that creates the change on the sandbox copy",
  "rollback_sql": "T-SQL that reverses it",
  "target_object": "name of index or procedure affected"
}
If no safe change remains, return kind="none" with empty SQL fields."""


def build_user_prompt(proc_text: str, scores: list[PlanScore]) -> str:
    findings = []
    for s in scores:
        findings.append({
            "combo": s.combo_label,
            "score": round(s.score, 1),
            "warnings": s.warnings,
            "missing_indexes": s.missing_indexes,
            "signals": s.signals,
        })
    return json.dumps({
        "procedure_definition": proc_text,
        "workload_analysis": findings,
    }, indent=2)


def _parse_change(raw: str) -> Change:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    data = json.loads(raw)
    return Change(
        kind=data.get("kind", "none"),
        rationale=data.get("rationale", ""),
        apply_sql=data.get("apply_sql", ""),
        rollback_sql=data.get("rollback_sql", ""),
        target_object=data.get("target_object", ""),
    )


class LLMBackend(Protocol):
    def propose_change(self, proc_text: str, scores: list[PlanScore]) -> Change: ...


class GeminiBackend:
    """Vertex AI Gemini backend — matches the user's existing stack."""

    def __init__(self, model: str = "gemini-1.5-flash", project: Optional[str] = None,
                 location: str = "us-central1"):
        self.model_name = model
        self.project = project
        self.location = location

    def propose_change(self, proc_text: str, scores: list[PlanScore]) -> Change:
        import vertexai
        from vertexai.generative_models import GenerativeModel

        vertexai.init(project=self.project, location=self.location)
        model = GenerativeModel(self.model_name, system_instruction=SYSTEM_PROMPT)
        resp = model.generate_content(
            build_user_prompt(proc_text, scores),
            generation_config={"temperature": 0.2, "response_mime_type": "application/json"},
        )
        return _parse_change(resp.text)


class FileBackend:
    """Replays a list of pre-decided changes from a JSON file.

    Used when the decision step is made by an external agent (e.g. Claude Code
    driving Microsoft Learn doc lookups) rather than an in-process API call —
    no ANTHROPIC_API_KEY required. The file is a JSON array of change objects in
    the same shape ClaudeBackend would emit; each call to ``propose_change``
    returns the next one. When the list is exhausted it returns ``kind="none"``
    so the loop terminates cleanly.
    """

    def __init__(self, path: str):
        with open(path) as f:
            self._decisions = json.load(f)
        self._i = 0

    def propose_change(self, proc_text: str, scores: list[PlanScore]) -> Change:
        if self._i >= len(self._decisions):
            return Change(kind="none", rationale="no further staged change", apply_sql="",
                          rollback_sql="", target_object="")
        data = self._decisions[self._i]
        self._i += 1
        return Change(
            kind=data.get("kind", "none"),
            rationale=data.get("rationale", ""),
            apply_sql=data.get("apply_sql", ""),
            rollback_sql=data.get("rollback_sql", ""),
            target_object=data.get("target_object", ""),
        )


class ClaudeBackend:
    """Anthropic Claude backend."""

    def __init__(self, model: str = "claude-sonnet-4-6"):
        self.model_name = model

    def propose_change(self, proc_text: str, scores: list[PlanScore]) -> Change:
        import anthropic

        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=self.model_name,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_prompt(proc_text, scores)}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        return _parse_change(text)
