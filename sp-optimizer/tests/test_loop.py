"""Offline tests for the optimization loop's verify/rollback behaviour.

The DB-facing steps (discover, capture, analyze, sandbox creation) are
monkeypatched with scripted results so the loop's control flow — the regression
gate, rollback execution, attempt history, and end-of-run cleanup — is tested
deterministically without a SQL Server.
"""
from __future__ import annotations

from scripts import optimize
from scripts.models import Change, ParamCombo, PlanScore


class FakeCursor:
    """Records every executed statement; returns nothing."""

    def __init__(self):
        self.executed: list[str] = []

    def execute(self, sql, *params):
        self.executed.append(sql)

    def fetchone(self):
        return None


class ScriptedBackend:
    """Returns a scripted list of changes, then kind='none'."""

    def __init__(self, changes: list[Change]):
        self.changes = list(changes)
        self.contexts = []

    def propose_change(self, proc_text, scores, context=None):
        self.contexts.append(context)
        if self.changes:
            return self.changes.pop(0)
        return Change(kind="none", rationale="done", apply_sql="",
                      rollback_sql="", target_object="")


def _combo(label):
    return ParamCombo(values={}, label=label)


def _scores(pairs):
    return [PlanScore(combo_label=l, score=s) for l, s in pairs]


def _run(tmp_path, monkeypatch, scripted_scores, backend, **kwargs):
    """Run the loop with every DB-facing step replaced by scripted results."""
    combos = [_combo("c1"), _combo("c2")]
    score_iter = iter(scripted_scores)

    monkeypatch.setattr(optimize.discover, "discover",
                        lambda cursor, proc, max_combos=12: ([], combos))
    monkeypatch.setattr(optimize.capture, "capture_workload",
                        lambda cursor, proc, combos, actual=False: [])
    monkeypatch.setattr(optimize.analyze, "analyze_workload",
                        lambda caps: next(score_iter))
    monkeypatch.setattr(optimize, "get_proc_text",
                        lambda cursor, proc: f"CREATE PROCEDURE {proc} AS SELECT 1;")
    monkeypatch.setattr(optimize, "make_sandbox",
                        lambda cursor, src, base, v: f"dbo.p_opt_v{v}")

    cursor = FakeCursor()
    run = optimize.RunDir(str(tmp_path), "dbo.p")
    try:
        history = optimize.run_loop(cursor, "dbo.p", backend, run, **kwargs)
    finally:
        run.close()
    return history, cursor, run


def _change(n, kind="index"):
    return Change(kind=kind, rationale=f"change {n}",
                  apply_sql=f"CREATE INDEX ix_{n} ON t(c);",
                  rollback_sql=f"DROP INDEX ix_{n} ON t;",
                  target_object=f"ix_{n}")


def test_regression_gate_rolls_back_and_reverts(tmp_path, monkeypatch):
    """A change that lifts one combo but tanks another beyond tolerance is
    rolled back, the iteration is invalidated, and the loop reverts to the
    previous variant for the next decision."""
    backend = ScriptedBackend([_change(1)])
    history, cursor, _ = _run(
        tmp_path, monkeypatch,
        scripted_scores=[
            _scores([("c1", 70), ("c2", 60)]),   # iter 0: baseline
            _scores([("c1", 95), ("c2", 20)]),   # iter 1: c2 regressed 40 pts
        ],
        backend=backend,
        max_iterations=3, target_fraction=0.99, regression_tolerance=10.0,
    )
    assert any("DROP INDEX ix_1" in sql for sql in cursor.executed), \
        "rollback SQL of the regressing change must be executed"
    assert history[0].change_rolled_back is True
    assert history[1].variant_invalidated is True
    assert history[1].regressions and "c2" in history[1].regressions[0]
    # after the rollback, the second decision was made against the ORIGINAL proc
    assert backend.contexts[1].attempts[-1].outcome == "rolled_back"


def test_kept_change_recorded_in_attempts(tmp_path, monkeypatch):
    backend = ScriptedBackend([_change(1), _change(2)])
    history, cursor, _ = _run(
        tmp_path, monkeypatch,
        scripted_scores=[
            _scores([("c1", 60), ("c2", 60)]),   # iter 0
            _scores([("c1", 70), ("c2", 65)]),   # iter 1: improved, no regression
            _scores([("c1", 72), ("c2", 66)]),   # iter 2
        ],
        backend=backend,
        max_iterations=3, target_fraction=0.99, regression_tolerance=10.0,
    )
    kept = [a for a in backend.contexts[-1].attempts if a.outcome == "kept"]
    assert kept and kept[0].kind == "index"


def test_sandbox_name_is_passed_to_decision(tmp_path, monkeypatch):
    backend = ScriptedBackend([])
    _run(
        tmp_path, monkeypatch,
        scripted_scores=[_scores([("c1", 60), ("c2", 60)])],
        backend=backend,
        max_iterations=1, target_fraction=0.99,
    )
    assert backend.contexts[0].sandbox_proc == "dbo.p_opt_v1"


def test_end_of_run_rolls_back_non_winning_changes(tmp_path, monkeypatch):
    """A change applied after the best iteration (never verified as better)
    is rolled back at end of run; the change that produced the winner stays."""
    backend = ScriptedBackend([_change(1), _change(2)])
    history, cursor, _ = _run(
        tmp_path, monkeypatch,
        scripted_scores=[
            _scores([("c1", 60), ("c2", 60)]),   # iter 0 -> change 1 applied
            _scores([("c1", 72), ("c2", 68)]),   # iter 1 (best) -> change 2 applied
        ],
        backend=backend,
        max_iterations=2, target_fraction=0.99, regression_tolerance=10.0,
    )
    # change 2 was applied at the best iteration -> not in its definition -> undone
    assert any("DROP INDEX ix_2" in sql for sql in cursor.executed)
    # change 1 produced the winner -> must NOT be rolled back
    assert not any("DROP INDEX ix_1" in sql for sql in cursor.executed)
    assert history[1].change_rolled_back is True
    assert history[0].change_rolled_back is False


def test_target_met_stops_immediately(tmp_path, monkeypatch):
    backend = ScriptedBackend([_change(1)])
    history, cursor, _ = _run(
        tmp_path, monkeypatch,
        scripted_scores=[_scores([("c1", 90), ("c2", 85)])],
        backend=backend,
        max_iterations=5, target_fraction=0.8,
    )
    assert len(history) == 1
    assert history[0].change_applied is None
    assert backend.changes, "no change should have been requested"


def test_apply_failure_continues_with_failed_attempt(tmp_path, monkeypatch):
    bad = Change(kind="rewrite", rationale="bad sql", apply_sql="THIS IS NOT SQL",
                 rollback_sql="", target_object="dbo.p_opt_v1")

    class ExplodingCursor(FakeCursor):
        def execute(self, sql, *params):
            super().execute(sql, *params)
            if "THIS IS NOT SQL" in sql:
                raise RuntimeError("syntax error")

    combos = [_combo("c1"), _combo("c2")]
    score_iter = iter([
        _scores([("c1", 60), ("c2", 60)]),
        _scores([("c1", 60), ("c2", 60)]),
    ])
    monkeypatch.setattr(optimize.discover, "discover",
                        lambda cursor, proc, max_combos=12: ([], combos))
    monkeypatch.setattr(optimize.capture, "capture_workload",
                        lambda cursor, proc, combos, actual=False: [])
    monkeypatch.setattr(optimize.analyze, "analyze_workload",
                        lambda caps: next(score_iter))
    monkeypatch.setattr(optimize, "get_proc_text",
                        lambda cursor, proc: "CREATE PROCEDURE p AS SELECT 1;")
    monkeypatch.setattr(optimize, "make_sandbox",
                        lambda cursor, src, base, v: f"dbo.p_opt_v{v}")

    backend = ScriptedBackend([bad, _change(2)])
    cursor = ExplodingCursor()
    run = optimize.RunDir(str(tmp_path), "dbo.p")
    try:
        history = optimize.run_loop(cursor, "dbo.p", backend, run,
                                    max_iterations=2, target_fraction=0.99)
    finally:
        run.close()
    # the failed attempt is reported to the next decision
    failed = [a for a in backend.contexts[-1].attempts if a.outcome == "failed"]
    assert failed and "syntax error" in failed[0].detail
    assert "apply failed" in history[0].regressions[0]


def test_winner_excludes_invalidated_variant(tmp_path):
    """write_winner must never pick a variant whose producing change was
    rolled back, even if its aggregate score is the highest."""
    from scripts.evidence import RunDir
    from scripts.models import IterationResult

    r0 = IterationResult(iteration=0, scores=[], aggregate_score=60.0,
                         fraction_good=0.5, proc_def="CREATE PROC a AS SELECT 0;")
    r0.change_applied = _change(1)
    r0.change_rolled_back = True
    r1 = IterationResult(iteration=1, scores=[], aggregate_score=99.0,
                         fraction_good=0.5, proc_def="CREATE PROC b AS SELECT 1;",
                         variant_invalidated=True)
    run = RunDir(str(tmp_path), "dbo.p")
    try:
        best = run.write_winner([r0, r1])
    finally:
        run.close()
    assert best.iteration == 0
    text = run.winner_path.read_text()
    assert "SELECT 0" in text
