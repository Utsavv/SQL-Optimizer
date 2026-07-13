"""Shared data models for the SP optimizer loop."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import eligibility


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
    # True when the parameter is a user-defined TABLE type (a TVP). A TVP cannot
    # be represented as a scalar literal, so it needs a DECLARE+populate variable
    # or a curated fixture — never a NULL literal. Set from sys.types.is_table_type.
    is_table_type: bool = False
    # True when the parameter name marks it as carrying secret material
    # (password/token/key). Set during discovery via eligibility.is_sensitive_param.
    is_sensitive: bool = False


@dataclass
class ParamCombo:
    """One concrete set of argument values to invoke the proc with."""
    values: dict[str, Any]    # {"@MemberId": 1023, "@FromDate": "2024-01-01"}
    label: str = ""           # human-readable tag e.g. "high-cardinality member"
    weight: float = 1.0       # how representative this combo is of real traffic
    # Eligibility of this combo for actual capture. ``ok`` combos are executed
    # and scored; any other status (invalid_input, requires_curated_workload,
    # requires_sensitive_input, ...) marks the combo as NOT a scorable plan — it
    # is carried through evidence but never counted as a bad query plan.
    status: str = eligibility.OK
    status_reason: str = ""


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
    # Analysis status. ``analyzed`` means the score is a real plan quality signal.
    # Anything else (capture_failed, not_analyzable, blocked_prerequisite,
    # invalid_input, requires_setup, requires_curated_workload,
    # requires_sensitive_input, timeout, ...) means the score is NOT a plan-
    # quality measurement and must be excluded from aggregate/fraction/decisions.
    status: str = "analyzed"
    status_reason: str = ""
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


def scorable_scores(scores: list[PlanScore]) -> list[PlanScore]:
    """The subset of scores that are genuine plan-quality measurements.

    A capture failure, a missing-feature block, an invalid synthetic input, or a
    call that needs curated/sensitive input is NOT a bad plan — it produced no
    trustworthy score. Only ``analyzed`` scores feed the aggregate, the good
    fraction, and any decision to change the procedure.
    """
    return [s for s in scores if s.status == "analyzed"]


def workload_score(scores: list[PlanScore], combos: list[ParamCombo]) -> float:
    """Weighted average plan score across the SCORABLE workload."""
    scores = scorable_scores(scores)
    if not scores:
        return 0.0
    weight_by_label = {c.label: c.weight for c in combos}
    total_w = sum(weight_by_label.get(s.combo_label, 1.0) for s in scores)
    if total_w == 0:
        return sum(s.score for s in scores) / len(scores)
    return sum(s.score * weight_by_label.get(s.combo_label, 1.0) for s in scores) / total_w


def fraction_good(scores: list[PlanScore], threshold: float) -> float:
    """Fraction of SCORABLE plans scoring at or above the quality threshold."""
    scores = scorable_scores(scores)
    if not scores:
        return 0.0
    return sum(1 for s in scores if s.score >= threshold) / len(scores)


# ---- workload quality + eligibility gating ---------------------------------
#
# A high plan score is only meaningful if it was measured on a REPRESENTATIVE
# call. Two conditions make the loop's conclusion untrustworthy no matter how
# good the score looks, and both must block an unqualified ``target_met`` /
# ``apply``:
#   * no scorable plan at all (every capture failed / was ineligible), and
#   * every scorable actual call returned zero rows (an all-empty workload).


# Priority order for reporting the dominant non-plan condition when nothing was
# scorable — the most actionable / specific cause wins.
_STATUS_PRIORITY = [
    eligibility.BLOCKED_PREREQUISITE,
    eligibility.REQUIRES_SETUP,
    eligibility.REQUIRES_SENSITIVE_INPUT,
    eligibility.REQUIRES_CURATED_WORKLOAD,
    eligibility.INVALID_INPUT,
    eligibility.TIMEOUT,
    eligibility.CANCELLED,
    eligibility.CAPTURE_FAILED,
    eligibility.NOT_ANALYZABLE,
]


def dominant_status(scores: list[PlanScore]) -> str:
    """The most significant non-scorable status across ``scores`` (for the
    terminal reason when no plan was analyzable)."""
    present = {s.status for s in scores if s.status != "analyzed"}
    for st in _STATUS_PRIORITY:
        if st in present:
            return st
    return eligibility.NOT_ANALYZABLE


def nonempty_output_fraction(scores: list[PlanScore]) -> Optional[float]:
    """Fraction of scorable calls that returned at least one row, or None when
    row counts are unknown (estimated mode carries no output_rows)."""
    sc = [s for s in scorable_scores(scores) if s.output_rows is not None]
    if not sc:
        return None
    return sum(1 for s in sc if (s.output_rows or 0) > 0) / len(sc)


@dataclass
class Termination:
    """The verdict for one evaluated iteration: whether to stop, why, and whether
    the procedure is even eligible for an optimization change."""
    stop: bool
    reason: Optional[str]              # e.g. "target_met", "not_analyzable", "empty_workload"
    terminal_status: str              # a truthful eligibility label for the report
    eligible_for_apply: bool          # may the agent be asked to change the proc?
    representative: bool              # was the workload representative (non-empty)?
    note: str = ""


def decide_termination(
    it: int,
    scores: list[PlanScore],
    aggregate: float,
    frac_good: float,
    *,
    target_fraction: float,
    max_iterations: int,
    prev_aggregate: float,
    stall_streak: int,
    use_actual: bool,
    allow_empty: bool = False,
    proc_block: Optional[tuple] = None,
) -> tuple[Termination, int]:
    """Central, shared termination + eligibility gate (used by both the agent-
    driven session and the in-process loop).

    Returns ``(Termination, new_stall_streak)``. It refuses to declare
    ``target_met`` — and refuses to mark the proc eligible for a change — unless a
    representative, analyzable workload actually backs the numbers.

    ``proc_block`` is an optional ``(status, reason)`` from a discovery-time
    precondition (missing feature, required setup, curated/sensitive input) that
    blocks the whole run before scoring even matters.
    """
    # 0. A discovery-time precondition blocks the entire run.
    if proc_block is not None:
        status, reason = proc_block
        return Termination(
            stop=True, reason=status, terminal_status=status,
            eligible_for_apply=False, representative=False, note=reason,
        ), stall_streak

    scorable = scorable_scores(scores)

    # 1. Nothing was analyzable → truthful not_analyzable / capture_failed / etc.
    if not scorable:
        status = dominant_status(scores) if scores else eligibility.NOT_ANALYZABLE
        note = ""
        for s in scores:
            if s.status == status and s.status_reason:
                note = s.status_reason
                break
        return Termination(
            stop=True, reason=status, terminal_status=status,
            eligible_for_apply=False, representative=False, note=note,
        ), stall_streak

    # 2. Workload representativeness (actual mode): an all-empty workload cannot
    #    satisfy the target as if real behavior had been measured.
    nonempty = nonempty_output_fraction(scores)
    representative = True
    if use_actual and nonempty is not None and nonempty == 0.0:
        representative = False
        if not allow_empty:
            return Termination(
                stop=True, reason="empty_workload",
                terminal_status="empty_workload", eligible_for_apply=False,
                representative=False,
                note=("every actual call in the workload returned zero rows; the "
                      "measured plans are not representative. Provide curated "
                      "values (SP_OPT_COMBOS) or set SP_OPT_ALLOW_EMPTY=1 to accept "
                      "an intentionally empty-workload test."),
            ), stall_streak

    # 3. Normal quality-based termination.
    reason = None
    if frac_good >= target_fraction:
        reason = "target_met"
    else:
        if aggregate > prev_aggregate + 0.5:
            stall_streak = 0
        else:
            stall_streak += 1
        if it > 0 and stall_streak >= 2:
            reason = "stalled"
        elif it + 1 >= max_iterations:
            reason = "max_iterations"

    terminal_status = "target_met" if reason == "target_met" else "optimizable"
    return Termination(
        stop=reason is not None, reason=reason, terminal_status=terminal_status,
        eligible_for_apply=reason is None, representative=representative,
        note="" if representative else "workload had no non-empty calls",
    ), stall_streak
