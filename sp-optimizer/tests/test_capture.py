"""Capture-orchestration tests: ineligible combos are never executed (Issues
5/6/10/11), and a shared server prerequisite failure short-circuits the rest of
the workload (Issue 4)."""
from scripts import capture, eligibility
from scripts.models import ParamCombo


def _combo(label, status=eligibility.OK):
    return ParamCombo(values={"@x": 1}, label=label, status=status)


def test_ineligible_combos_are_not_executed(monkeypatch):
    executed = []

    def fake_actual(cursor, proc, combo, timeout=None):
        executed.append(combo.label)
        from scripts.models import PlanCapture
        return PlanCapture(combo=combo, plan_xml="<x/>")

    monkeypatch.setattr(capture, "capture_actual", fake_actual)
    combos = [
        _combo("ok1"),
        _combo("bad", status=eligibility.INVALID_INPUT),
        _combo("ok2"),
    ]
    caps = capture.capture_workload(None, "dbo.P", combos, actual=True)
    assert executed == ["ok1", "ok2"]           # the invalid combo never ran
    assert len(caps) == 3
    # the skipped one carries no plan (analysis will read its combo status)
    assert caps[1].plan_xml == ""


def test_full_text_failure_short_circuits_remaining(monkeypatch):
    executed = []

    def fake_actual(cursor, proc, combo, timeout=None):
        executed.append(combo.label)
        from scripts.models import PlanCapture
        return PlanCapture(combo=combo, plan_xml="",
                           error="Full-Text Search is not installed (7609)")

    monkeypatch.setattr(capture, "capture_actual", fake_actual)
    combos = [_combo(f"c{i}") for i in range(5)]
    caps = capture.capture_workload(None, "dbo.P", combos, actual=True)
    # Only the first combo actually executes; the rest are skipped after the
    # deterministic environment failure.
    assert executed == ["c0"]
    for cap in caps[1:]:
        assert "prerequisite" in (cap.error or "")
        # classified consistently as the same blocking prerequisite
        assert eligibility.classify_sql_error(cap.error)[0] == eligibility.BLOCKED_PREREQUISITE


def test_estimated_mode_still_skips_ineligible(monkeypatch):
    executed = []

    def fake_est(cursor, proc, combo):
        executed.append(combo.label)
        from scripts.models import PlanCapture
        return PlanCapture(combo=combo, plan_xml="<x/>")

    monkeypatch.setattr(capture, "capture_estimated", fake_est)
    combos = [_combo("ok"), _combo("bad", status=eligibility.REQUIRES_SENSITIVE_INPUT)]
    capture.capture_workload(None, "dbo.P", combos, actual=False)
    assert executed == ["ok"]
