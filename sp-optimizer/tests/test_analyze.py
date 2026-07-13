"""Analysis-status tests: a capture failure / ineligible combo is classified,
not scored as a bad plan (Issues 3, 4, 5, 6, 9, 11, 12)."""
from scripts import analyze, eligibility
from scripts.models import ParamCombo, PlanCapture


def _cap(label="c", **kw):
    combo = ParamCombo(values=kw.pop("values", {}), label=label,
                       status=kw.pop("status", eligibility.OK),
                       status_reason=kw.pop("status_reason", ""))
    return PlanCapture(combo=combo, plan_xml=kw.pop("plan_xml", ""), **kw)


def test_empty_plan_no_error_is_capture_failed():
    """uspLogError-style: no ShowPlan XML, no error → not a score of zero."""
    score = analyze.analyze_plan(_cap(plan_xml=""))
    assert score.status == eligibility.CAPTURE_FAILED
    assert score.score == 0.0
    assert "empty plan" in " ".join(score.warnings).lower()


def test_full_text_error_is_blocked_prerequisite():
    score = analyze.analyze_plan(_cap(error="Full-Text Search is not installed (7609)"))
    assert score.status == eligibility.BLOCKED_PREREQUISITE


def test_tinyint_conversion_error_is_invalid_input():
    score = analyze.analyze_plan(_cap(error="Error converting data type int to tinyint."))
    assert score.status == eligibility.INVALID_INPUT


def test_special_principal_error_is_curated():
    score = analyze.analyze_plan(_cap(error="Cannot use the special principal 'db_backupoperator'."))
    assert score.status == eligibility.REQUIRES_CURATED_WORKLOAD


def test_temporal_error_is_requires_setup():
    score = analyze.analyze_plan(_cap(error="SYSTEM_TIME period is already defined"))
    assert score.status == eligibility.REQUIRES_SETUP


def test_tvp_clash_error_is_curated():
    score = analyze.analyze_plan(_cap(error="Operand type clash: NULL is incompatible with OrderList"))
    assert score.status == eligibility.REQUIRES_CURATED_WORKLOAD


def test_unknown_error_is_capture_failed():
    score = analyze.analyze_plan(_cap(error="something weird happened"))
    assert score.status == eligibility.CAPTURE_FAILED


def test_ineligible_combo_status_passes_through():
    """A combo discovery flagged invalid is never executed/scored as a plan."""
    cap = _cap(status=eligibility.INVALID_INPUT, status_reason="@x=1000 bad for tinyint")
    score = analyze.analyze_plan(cap)
    assert score.status == eligibility.INVALID_INPUT
    assert "tinyint" in score.status_reason


_GOOD_PLAN = (
    '<?xml version="1.0"?>'
    '<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan">'
    '<BatchSequence><Batch><Statements><StmtSimple>'
    '<QueryPlan><RelOp PhysicalOp="Index Seek" EstimateRows="5"/></QueryPlan>'
    '</StmtSimple></Statements></Batch></BatchSequence></ShowPlanXML>'
)


def test_valid_plan_is_analyzed():
    score = analyze.analyze_plan(_cap(plan_xml=_GOOD_PLAN))
    assert score.status == "analyzed"
    assert score.score > 0
