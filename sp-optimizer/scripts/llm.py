"""Step 4 backend: the LLM decision step.

The ONLY place a model is required. Given the current procedure text and the
analyzed workload, the model proposes a single smallest-safe change as strict
JSON. The default backend goes through LiteLLM, so the provider (OpenAI,
Anthropic, Gemini, Azure, Bedrock, ...) is just a model-string + API-key
choice — no code change needed to switch.

The prompt deliberately constrains the model to ONE change per iteration with a
rollback, so the loop stays auditable and reversible.
"""
from __future__ import annotations

import json
import os
from typing import Optional, Protocol

from .models import Change, DecisionContext, PlanScore


SYSTEM_PROMPT = """You are a senior SQL Server performance engineer.
You are given a stored procedure and a deterministic analysis of its execution
plans across a representative set of parameter values. Propose exactly ONE
smallest, safest change that will improve the MAJORITY of the parameter calls
without regressing others.

The user message may also include:
  - "sandbox_procedure": the name of a pre-created sandbox COPY of the
    procedure. Any rewrite/hint change MUST alter this sandbox object, never
    the original procedure. Use exactly this name in apply_sql.
  - "previous_attempts": changes already tried this run and their outcome
    (kept / rolled_back / rejected / failed). NEVER re-propose a change that
    was rolled back, rejected, or failed — propose something different or
    return kind="none".
  - "static_review_findings": deterministic linter findings on the procedure
    text (non-SARGable predicates, type mismatches, catch-all filters...).
    Prefer fixing a root cause flagged there when it explains the plan
    warnings.
  - "query_store_plans": Query Store plan history for this procedure
    (query_id, plan_id, executions, avg duration). ONLY when this key is
    present you may return kind="force_plan" with apply_sql calling
    sys.sp_query_store_force_plan for a demonstrably better historical plan
    (rollback via sys.sp_query_store_unforce_plan).

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
  "kind": "option_hint|index|rewrite|recompile|force_plan|none",
  "rationale": "one paragraph",
  "apply_sql": "T-SQL that creates the change on the sandbox copy",
  "rollback_sql": "T-SQL that reverses it",
  "target_object": "name of index or procedure affected"
}
If no safe change remains, return kind="none" with empty SQL fields."""


def build_user_prompt(
    proc_text: str,
    scores: list[PlanScore],
    context: Optional[DecisionContext] = None,
) -> str:
    findings = []
    for s in scores:
        findings.append({
            "combo": s.combo_label,
            "score": round(s.score, 1),
            "warnings": s.warnings,
            "missing_indexes": s.missing_indexes,
            "signals": s.signals,
        })
    payload: dict = {
        "procedure_definition": proc_text,
        "workload_analysis": findings,
    }
    if context is not None:
        if context.sandbox_proc:
            payload["sandbox_procedure"] = context.sandbox_proc
        if context.attempts:
            payload["previous_attempts"] = [
                {
                    "iteration": a.iteration,
                    "kind": a.kind,
                    "target_object": a.target_object,
                    "outcome": a.outcome,
                    "detail": a.detail,
                }
                for a in context.attempts
            ]
        if context.review_findings:
            payload["static_review_findings"] = [
                {"rule": f.rule, "severity": f.severity, "message": f.message}
                for f in context.review_findings
            ]
        if context.query_store_plans:
            payload["query_store_plans"] = context.query_store_plans
    return json.dumps(payload, indent=2, default=str)


def _parse_change(raw: str) -> Change:
    """Parse the model's reply into a Change.

    Models wrap JSON in code fences or prose despite instructions, so after
    stripping fences we fall back to the outermost {...} span. Raises
    ValueError when no JSON object can be recovered at all."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"no JSON object in LLM reply: {raw[:200]!r}")
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError as e:
            raise ValueError(f"malformed JSON in LLM reply: {e}") from e
    return Change(
        kind=data.get("kind", "none"),
        rationale=data.get("rationale", ""),
        apply_sql=data.get("apply_sql", ""),
        rollback_sql=data.get("rollback_sql", ""),
        target_object=data.get("target_object", ""),
    )


class LLMBackend(Protocol):
    def propose_change(
        self,
        proc_text: str,
        scores: list[PlanScore],
        context: Optional[DecisionContext] = None,
    ) -> Change: ...


class LiteLLMBackend:
    """Provider-agnostic backend, routed through LiteLLM.

    The provider is selected entirely by the ``model`` string (e.g.
    ``"ollama_chat/gemma4"``, ``"gemini/gemini-1.5-flash"``,
    ``"claude-3-5-sonnet-20241022"``, ``"gpt-4o"``) and the matching API key
    in the environment — no code change is needed to switch providers. See
    README.md for the model-string / env-var mapping for each provider.

    Defaults to a local Ollama model (``ollama_chat/gemma4`` against
    ``http://localhost:11434``, the same ``/api/chat`` endpoint used by
    ``curl http://localhost:11434/api/chat``), so the loop runs out of the
    box with no API key as long as Ollama is running locally.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: float = 0.2,
        api_base: Optional[str] = None,
    ):
        self.model_name = model or os.environ.get("LLM_MODEL", "ollama_chat/gemma4")
        self.temperature = temperature
        self.api_base = api_base or os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")

    def propose_change(
        self,
        proc_text: str,
        scores: list[PlanScore],
        context: Optional[DecisionContext] = None,
    ) -> Change:
        from litellm import completion

        kwargs = {}
        if self.model_name.startswith("ollama"):
            kwargs["api_base"] = self.api_base

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(proc_text, scores, context)},
        ]
        # One repair round-trip on malformed output, then degrade to kind="none"
        # so a flaky model ends the loop cleanly instead of crashing the run.
        last_err = ""
        for attempt in range(2):
            resp = completion(
                model=self.model_name,
                messages=messages,
                temperature=self.temperature,
                stream=False,
                **kwargs,
            )
            text = resp.choices[0].message.content or ""  # type: ignore[union-attr]
            try:
                return _parse_change(text)
            except ValueError as e:
                last_err = str(e)
                messages.append({"role": "assistant", "content": text})
                messages.append({
                    "role": "user",
                    "content": "Your previous reply was not valid JSON. Reply again "
                               "with ONLY the strict JSON object described in the "
                               "system prompt — no prose, no code fences.",
                })
        return Change(kind="none",
                      rationale=f"LLM returned unparseable output twice: {last_err}",
                      apply_sql="", rollback_sql="", target_object="")


class FileBackend:
    """Replays a list of pre-decided changes from a JSON file.

    Used when the decision step is made by an external agent (e.g. Claude Code
    driving Microsoft Learn doc lookups) rather than an in-process API call —
    no LLM API key required. The file is a JSON array of change objects in
    the same shape LiteLLMBackend would emit; each call to ``propose_change``
    returns the next one. When the list is exhausted it returns ``kind="none"``
    so the loop terminates cleanly.
    """

    def __init__(self, path: str):
        with open(path) as f:
            self._decisions = json.load(f)
        self._i = 0

    def propose_change(
        self,
        proc_text: str,
        scores: list[PlanScore],
        context: Optional[DecisionContext] = None,
    ) -> Change:
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
