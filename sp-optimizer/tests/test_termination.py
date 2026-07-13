"""Termination + eligibility gate tests (Issues 2, 3, 4, 7, 9-12).

A high plan score is only allowed to mean ``target_met`` when a representative,
analyzable workload actually backs it.
"""
from scripts import eligibility
from scripts.models import (
    PlanScore,
    ParamCombo,
    decide_termination,
    fraction_good,
    workload_score,
)


def _score(label, score=100.0, status="analyzed", output_rows=None):
    return PlanScore(combo_label=label, score=score, status=status, output_rows=output_rows)


def _combos(n):
    return [ParamCombo(values={}, label=f"c{i}", weight=1.0) for i in range(n)]


BASE = dict(target_fraction=0.8, max_iterations=5, prev_aggregate=-1.0,
            stall_streak=0)


# ---- scorable filtering (Issues 3/5/6): non-plan statuses excluded ---------

def test_workload_score_excludes_non_scorable():
    scores = [_score("a", 100), _score("b", 0, status=eligibility.INVALID_INPUT)]
    combos = _combos(2)
    combos[0].label, combos[1].label = "a", "b"
    assert workload_score(scores, combos) == 100.0
    assert fraction_good(scores, 75.0) == 1.0  # only the analyzed one counts


# ---- Issue 2: all-empty actual workload cannot be target_met ---------------

def test_all_empty_actual_blocks_target_met():
    scores = [_score(f"c{i}", 100.0, output_rows=0) for i in range(4)]
    term, _ = decide_termination(0, scores, 100.0, 1.0, use_actual=True, **BASE)
    assert term.reason == "empty_workload"
    assert term.terminal_status == "empty_workload"
    assert not term.eligible_for_apply
    assert not term.representative


def test_all_empty_actual_allowed_with_optin():
    scores = [_score(f"c{i}", 100.0, output_rows=0) for i in range(4)]
    term, _ = decide_termination(0, scores, 100.0, 1.0, use_actual=True,
                                 allow_empty=True, **BASE)
    assert term.reason == "target_met"
    assert not term.representative  # honestly recorded as non-representative


def test_nonempty_actual_reaches_target_met():
    scores = [_score(f"c{i}", 100.0, output_rows=10) for i in range(4)]
    term, _ = decide_termination(0, scores, 100.0, 1.0, use_actual=True, **BASE)
    assert term.reason == "target_met"
    assert term.representative


def test_estimated_mode_ignores_empty_gate():
    # estimated plans carry no output_rows → representativeness is unknown, not
    # a failure.
    scores = [_score(f"c{i}", 100.0, output_rows=None) for i in range(4)]
    term, _ = decide_termination(0, scores, 100.0, 1.0, use_actual=False, **BASE)
    assert term.reason == "target_met"


# ---- Issue 3: no analyzable plan → not_analyzable, not a score of zero ------

def test_all_capture_failed_is_not_analyzable():
    scores = [_score(f"c{i}", 0.0, status=eligibility.CAPTURE_FAILED) for i in range(3)]
    term, _ = decide_termination(0, scores, 0.0, 0.0, use_actual=True, **BASE)
    assert term.terminal_status == eligibility.CAPTURE_FAILED
    assert not term.eligible_for_apply
    assert term.stop


def test_all_blocked_prerequisite_dominates():
    scores = [_score(f"c{i}", 0.0, status=eligibility.BLOCKED_PREREQUISITE) for i in range(3)]
    term, _ = decide_termination(0, scores, 0.0, 0.0, use_actual=True, **BASE)
    assert term.terminal_status == eligibility.BLOCKED_PREREQUISITE
    assert not term.eligible_for_apply


# ---- proc-level precondition block (Issues 4/7/9/10/11/12) ------------------

def test_proc_block_stops_before_scoring():
    term, _ = decide_termination(
        0, [], 0.0, 0.0, use_actual=True,
        proc_block=(eligibility.REQUIRES_SETUP, "needs predecessor"), **BASE)
    assert term.terminal_status == eligibility.REQUIRES_SETUP
    assert not term.eligible_for_apply
    assert term.stop


# ---- normal optimizable path -----------------------------------------------

def test_low_score_stays_optimizable_and_eligible():
    scores = [_score(f"c{i}", 40.0, output_rows=10) for i in range(4)]
    term, _ = decide_termination(0, scores, 40.0, 0.0, use_actual=True, **BASE)
    assert term.reason is None
    assert term.eligible_for_apply
    assert term.terminal_status == "optimizable"


def test_stall_terminates_after_two_rounds():
    scores = [_score(f"c{i}", 40.0, output_rows=10) for i in range(4)]
    term, streak = decide_termination(
        2, scores, 40.0, 0.0, use_actual=True,
        target_fraction=0.8, max_iterations=5, prev_aggregate=40.0, stall_streak=1)
    assert term.reason == "stalled"
    assert not term.eligible_for_apply
