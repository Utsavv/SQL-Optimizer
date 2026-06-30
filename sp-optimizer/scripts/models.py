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
    kind: str                 # "index" | "option_hint" | "rewrite" | "recompile" | "none"
    rationale: str
    apply_sql: str            # SQL that effects the change (on the sandbox copy)
    rollback_sql: str
    target_object: str = ""   # e.g. proc name or new index name


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


def workload_score(scores: list[PlanScore], combos: list[ParamCombo]) -> float:
    """Weighted average plan score across the workload."""
    if not scores:
        return 0.0
    weight_by_label = {c.label: c.weight for c in combos}
    total_w = sum(weight_by_label.get(s.combo_label, 1.0) for s in scores)
    if total_w == 0:
        return sum(s.score for s in scores) / len(scores)
    return sum(s.score * weight_by_label.get(s.combo_label, 1.0) for s in scores) / total_w


def fraction_good(scores: list[PlanScore], threshold: float) -> float:
    """Fraction of plans scoring at or above the quality threshold."""
    if not scores:
        return 0.0
    return sum(1 for s in scores if s.score >= threshold) / len(scores)
