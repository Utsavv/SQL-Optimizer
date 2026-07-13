"""End-to-end agent-driven session flow (Issues 2, 3, 7, 9).

Drives discover → evaluate → finish through a real session.json round-trip with a
mock cursor and stubbed capture, asserting that ineligible / unrepresentative /
unanalyzable runs terminate at ``finish`` (never ``apply``) and still write the
documented evidence artifacts.
"""
import json

import pytest

from scripts import analyze, capture, discover, eligibility, session as sess
from scripts.evidence import RunDir
from scripts.models import ParamCombo, PlanCapture
from mockdb import MockCursor, Row


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in ("SP_OPT_COMBOS", "SP_OPT_ALLOW_EMPTY", "SP_OPT_ALLOW_BULK",
                "SP_OPT_SETUP_SQL", "SP_OPT_TEARDOWN_SQL"):
        monkeypatch.delenv(var, raising=False)


def _make_session(tmp_path, combos, proc_block=None, use_actual=True):
    run = RunDir(str(tmp_path / "out"), "dbo.P")
    config = {
        "target_fraction": 0.8, "quality_threshold": 75.0, "max_iterations": 5,
        "regression_tolerance": 10.0, "use_actual": use_actual, "max_combos": 12,
        "command_timeout": 30, "proc_block": list(proc_block) if proc_block else None,
    }
    s = sess.Session.create(run, "dbo.P", combos, config)
    run.close()
    return sess.Session.load(str(s.path))


def _stub_capture(monkeypatch, caps):
    monkeypatch.setattr(capture, "capture_workload", lambda *a, **k: caps)
    monkeypatch.setattr(sess, "get_proc_text", lambda c, p: "CREATE PROC dbo.P AS SELECT 1")


def _plan(rows):
    return (
        '<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan">'
        '<RelOp PhysicalOp="Index Seek" EstimateRows="5">'
        '<RunTimeInformation><RunTimeCountersPerThread ActualRows="%d"/>'
        '</RunTimeInformation></RelOp></ShowPlanXML>' % rows
    )


def test_empty_actual_workload_stops_at_finish(tmp_path, monkeypatch):
    combos = [ParamCombo(values={"@d": "2020-01-01"}, label=f"c{i}") for i in range(3)]
    # every call returns zero rows (output_rows is populated by capture in prod)
    caps = [PlanCapture(combo=c, plan_xml=_plan(0), output_rows=0) for c in combos]
    _stub_capture(monkeypatch, caps)
    s = _make_session(tmp_path, combos)
    out = sess.do_evaluate(s, MockCursor())
    assert out["terminal_status"] == "empty_workload"
    assert out["eligible_for_apply"] is False
    assert out["next_step"] == "finish"


def test_all_capture_failed_is_not_analyzable(tmp_path, monkeypatch):
    combos = [ParamCombo(values={}, label="no-params")]
    caps = [PlanCapture(combo=combos[0], plan_xml="")]  # no plan, no error
    _stub_capture(monkeypatch, caps)
    s = _make_session(tmp_path, combos)
    out = sess.do_evaluate(s, MockCursor())
    assert out["terminal_status"] == eligibility.CAPTURE_FAILED
    assert out["next_step"] == "finish"
    assert out["eligible_for_apply"] is False


def test_proc_block_skips_capture_entirely(tmp_path, monkeypatch):
    combos = [ParamCombo(values={}, label="c0")]
    # capture must NOT be called for a blocked proc (e.g. bulk generator).
    def _boom(*a, **k):
        raise AssertionError("capture must not run for a blocked proc")
    monkeypatch.setattr(capture, "capture_workload", _boom)
    monkeypatch.setattr(sess, "get_proc_text", lambda c, p: "WHILE 1=1 INSERT ...")
    s = _make_session(tmp_path, combos,
                      proc_block=(eligibility.REQUIRES_CURATED_WORKLOAD, "bulk generator"))
    out = sess.do_evaluate(s, MockCursor())
    assert out["terminal_status"] == eligibility.REQUIRES_CURATED_WORKLOAD
    assert out["next_step"] == "finish"


def test_representative_workload_can_optimize(tmp_path, monkeypatch):
    combos = [ParamCombo(values={"@d": "2020-01-01"}, label=f"c{i}") for i in range(3)]
    caps = [PlanCapture(combo=c, plan_xml=_plan(50), output_rows=50) for c in combos]
    _stub_capture(monkeypatch, caps)
    s = _make_session(tmp_path, combos)
    out = sess.do_evaluate(s, MockCursor())
    # good plans over a non-empty workload → target met, representative
    assert out["representative_workload"] is True
    assert out["terminal_status"] in ("target_met", "optimizable")


def test_finish_writes_artifacts_after_block(tmp_path, monkeypatch):
    combos = [ParamCombo(values={}, label="c0")]
    monkeypatch.setattr(sess, "get_proc_text", lambda c, p: "SELECT 1")
    monkeypatch.setattr(capture, "capture_workload", lambda *a, **k: [])
    s = _make_session(tmp_path, combos,
                      proc_block=(eligibility.REQUIRES_SETUP, "needs predecessor"))
    sess.do_evaluate(s, MockCursor())
    result = sess.do_finish(sess.Session.load(str(s.path)))
    # documented artifacts exist even for a blocked run
    from pathlib import Path
    for key in ("report", "changes", "winner", "manifest"):
        assert Path(result[key]).exists(), f"missing {key}"
    manifest = json.loads(Path(result["manifest"]).read_text())
    assert manifest["proc"] == "dbo.P"
