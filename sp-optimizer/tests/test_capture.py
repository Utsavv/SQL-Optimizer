"""Offline tests for capture helpers: repeated-run aggregation and wait deltas."""
from scripts import capture
from scripts.capture import _wait_delta, capture_actual_repeated
from scripts.models import ParamCombo, PlanCapture


def test_wait_delta_top_n_and_idle_filtered():
    before = {"PAGEIOLATCH_SH": 100.0, "WAITFOR": 5000.0}
    after = {"PAGEIOLATCH_SH": 400.0, "WAITFOR": 9000.0,
             "LCK_M_S": 50.0, "SOS_SCHEDULER_YIELD": 10.0}
    d = _wait_delta(before, after, top=2)
    assert d == {"PAGEIOLATCH_SH": 300.0, "LCK_M_S": 50.0}
    assert "WAITFOR" not in d


def test_wait_delta_none_when_snapshot_missing():
    assert _wait_delta(None, {"X": 1.0}) is None
    assert _wait_delta({"X": 1.0}, None) is None


def test_repeated_runs_report_medians(monkeypatch):
    combo = ParamCombo(values={}, label="c")
    elapsed = iter([999.0,           # warm-up (discarded)
                    100.0, 300.0, 200.0])  # measured runs

    def fake_actual(cursor, proc, cmb):
        e = next(elapsed)
        return PlanCapture(combo=cmb, plan_xml="<x/>", elapsed_ms=e,
                           cpu_ms=e / 2, logical_reads=int(e * 10))

    monkeypatch.setattr(capture, "capture_actual", fake_actual)
    cap = capture_actual_repeated(None, "dbo.p", combo, runs=3)
    assert cap.elapsed_ms == 200.0          # median of 100/300/200
    assert cap.cpu_ms == 100.0
    assert cap.logical_reads == 2000


def test_single_run_passthrough(monkeypatch):
    called = []

    def fake_actual(cursor, proc, cmb):
        called.append(1)
        return PlanCapture(combo=cmb, plan_xml="<x/>", elapsed_ms=42.0)

    monkeypatch.setattr(capture, "capture_actual", fake_actual)
    cap = capture_actual_repeated(None, "dbo.p", ParamCombo(values={}, label="c"), runs=1)
    assert len(called) == 1 and cap.elapsed_ms == 42.0


def test_all_runs_failed_returns_last(monkeypatch):
    def fake_actual(cursor, proc, cmb):
        return PlanCapture(combo=cmb, plan_xml="", error="boom")

    monkeypatch.setattr(capture, "capture_actual", fake_actual)
    cap = capture_actual_repeated(None, "dbo.p", ParamCombo(values={}, label="c"), runs=2)
    assert cap.error == "boom"
