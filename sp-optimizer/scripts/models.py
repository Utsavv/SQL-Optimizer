"""Shared data models for the SP optimizer loop."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ProcParam:
    """A single parameter of a stored procedure."""
    name: str                 # e.g. "@MemberId"
    sql_type: str             # e.g. "int", "varchar(50)", "datetime2"
    is_output: bool = False
    default: Optional[str] = None
    # True when the parameter declares a default (``@p ... = NULL``). Optional
    # params are the ones callers routinely omit, so their NULL branch is a
    # real, high-traffic path the workload must exercise. Set from
    # sys.parameters.has_default_value during discovery.
    has_default: bool = False


@dataclass
class ParamCombo:
    """One concrete set of argument values to invoke the proc with."""
    values: dict[str, Any]    # {"@MemberId": 1023, "@FromDate": "2024-01-01"}
    label: str = ""           # human-readable tag e.g. "high-cardinality member"
    weight: float = 1.0       # how representative this combo is of real traffic


@dataclass
class PlanCapture:
    """Result of running the proc with one ParamCombo."""
    combo: ParamCombo
    plan_xml: str
    elapsed_ms: Optional[float] = None
    cpu_ms: Optional[float] = None
    logical_reads: Optional[int] = None
    output_rows: Optional[float] = None
    error: Optional[str] = None
    # Raw SET STATISTICS IO / TIME text (actual mode only). This is the
    # human-readable IO-stat evidence the report links to alongside the plan.
    io_stats_text: Optional[str] = None


@dataclass
class PlanScore:
    """Deterministic analysis of a single execution plan."""
    combo_label: str
    score: float                       # 0..100, higher is better
    warnings: list[str] = field(default_factory=list)
    missing_indexes: list[str] = field(default_factory=list)
    # raw signals the LLM can reason over
    signals: dict[str, Any] = field(default_factory=dict)
    # runtime stats carried through from the capture (actual mode only)
    elapsed_ms: Optional[float] = None
    cpu_ms: Optional[float] = None
    logical_reads: Optional[int] = None
    output_rows: Optional[float] = None
    # paths (relative to the run dir) to the persisted evidence for this combo,
    # so the report can link straight to the raw plan XML / IO-stat text.
    plan_path: Optional[str] = None
    stats_path: Optional[str] = None


@dataclass
class Change:
    """A proposed (or applied) modification to the procedure."""
    kind: str                 # "index" | "option_hint" | "rewrite" | "recompile" | "force_plan" | "none"
    rationale: str
    apply_sql: str            # SQL that effects the change (on the sandbox copy)
    rollback_sql: str
    target_object: str = ""   # e.g. proc name or new index name


@dataclass
class ReviewFinding:
    """One finding from the deterministic T-SQL review step."""
    rule: str                 # e.g. "non_sargable_predicate"
    severity: str             # "high" | "medium" | "info"
    message: str
    snippet: str = ""         # the offending fragment, when text-anchored


@dataclass
class AttemptRecord:
    """One change the loop already tried, and what became of it.

    Fed back into the decision prompt so the model never re-proposes a change
    that was rejected, failed to apply, or was rolled back for regressing."""
    iteration: int
    kind: str
    target_object: str
    outcome: str              # "kept" | "rolled_back" | "rejected" | "failed"
    detail: str = ""          # score delta, regression list, or error text


@dataclass
class DecisionContext:
    """Everything the decision step may ground a proposal in, beyond the proc
    text and the current scores."""
    # Name of the pre-created sandbox copy the apply_sql MUST target for any
    # rewrite/hint change. Created before the decision so the model never has
    # to guess the sandbox name.
    sandbox_proc: str = ""
    # Every change already attempted this run, with its outcome.
    attempts: list[AttemptRecord] = field(default_factory=list)
    # Findings from the deterministic T-SQL review step (scripts/review.py).
    review_findings: list = field(default_factory=list)
    # Query Store plan summary rows (query_id/plan_id/executions/duration) when
    # plan forcing is allowed — lets the model propose kind="force_plan".
    query_store_plans: list[dict] = field(default_factory=list)


@dataclass
class IterationResult:
    """Everything that happened in one pass of the loop."""
    iteration: int
    scores: list[PlanScore]
    aggregate_score: float            # weighted workload score
    fraction_good: float              # fraction of combos at/above threshold
    change_applied: Optional[Change] = None
    regressions: list[str] = field(default_factory=list)
    # the procedure variant that was captured/scored this iteration, plus its
    # full definition — used to pick and write out the winning variant.
    scored_proc: str = ""
    proc_def: str = ""
    # True when change_applied was later undone (its verify pass regressed a
    # combo, or it wasn't part of the winner at end of run) — such a change is
    # NOT present in any surviving variant and must be excluded from winner.sql.
    change_rolled_back: bool = False
    # True when the variant scored THIS iteration was produced by a change that
    # got rolled back for regressing a combo — it fails the no-regression
    # contract and is never eligible to be the winner.
    variant_invalidated: bool = False


def workload_score(scores: list[PlanScore], combos: list[ParamCombo]) -> float:
    """Weighted average plan score across the workload."""
    if not scores:
        return 0.0
    weight_by_label = {c.label: c.weight for c in combos}
    total_w = sum(weight_by_label.get(s.combo_label, 1.0) for s in scores)
    if total_w == 0:
        return sum(s.score for s in scores) / len(scores)
    return sum(s.score * weight_by_label.get(s.combo_label, 1.0) for s in scores) / total_w


def fraction_good(
    scores: list[PlanScore], threshold: float, combos: Optional[list[ParamCombo]] = None
) -> float:
    """Fraction of plans scoring at or above the quality threshold.

    When combos are supplied the fraction is weighted the same way the
    aggregate score is, so "80% of combos good" means 80% of *representative
    traffic*, not 80% of a list where a rare edge case counts as much as the
    hot path."""
    if not scores:
        return 0.0
    if combos:
        weight_by_label = {c.label: c.weight for c in combos}
        total_w = sum(weight_by_label.get(s.combo_label, 1.0) for s in scores)
        if total_w > 0:
            good_w = sum(
                weight_by_label.get(s.combo_label, 1.0)
                for s in scores if s.score >= threshold
            )
            return good_w / total_w
    return sum(1 for s in scores if s.score >= threshold) / len(scores)
