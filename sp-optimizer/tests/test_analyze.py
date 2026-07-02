"""Offline tests for the deterministic plan analyzer, using canned ShowPlan XML."""
from scripts.analyze import analyze_plan
from scripts.models import ParamCombo, PlanCapture

_NS = 'xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan"'


def _cap(inner_xml: str, **kw) -> PlanCapture:
    xml = f'<ShowPlanXML {_NS}><BatchSequence><Batch><Statements>{inner_xml}</Statements></Batch></BatchSequence></ShowPlanXML>'
    return PlanCapture(combo=ParamCombo(values={}, label="t"), plan_xml=xml, **kw)


def _stmt(body: str, text: str = "SELECT 1", cost: str = "1.0") -> str:
    return (f'<StmtSimple StatementText="{text}" StatementSubTreeCost="{cost}">'
            f'<QueryPlan>{body}</QueryPlan></StmtSimple>')


def test_clean_plan_scores_100():
    s = analyze_plan(_cap(_stmt('<RelOp PhysicalOp="Index Seek" EstimateRows="10"/>')))
    assert s.score == 100.0
    assert not s.warnings


def test_large_scan_penalized_small_scan_not():
    big = analyze_plan(_cap(_stmt('<RelOp PhysicalOp="Clustered Index Scan" EstimateRows="500000"/>')))
    small = analyze_plan(_cap(_stmt('<RelOp PhysicalOp="Clustered Index Scan" EstimateRows="50"/>')))
    assert big.score < 100.0 and big.signals["table_scan_count"] == 1
    assert small.score == 100.0 and small.signals["table_scan_count"] == 0


def test_memory_grant_warning():
    s = analyze_plan(_cap(_stmt(
        '<RelOp PhysicalOp="Index Seek" EstimateRows="1">'
        '<Warnings><MemoryGrantWarning GrantWarningKind="Excessive Grant"/></Warnings>'
        '</RelOp>')))
    assert s.signals["memory_grant_warning_count"] == 1
    assert s.score < 100.0


def test_large_spool_flagged():
    s = analyze_plan(_cap(_stmt('<RelOp PhysicalOp="Table Spool" EstimateRows="50000"/>')))
    assert s.signals["spool_count"] == 1
    tiny = analyze_plan(_cap(_stmt('<RelOp PhysicalOp="Table Spool" EstimateRows="5"/>')))
    assert tiny.signals["spool_count"] == 0


def test_no_join_predicate():
    s = analyze_plan(_cap(_stmt(
        '<RelOp PhysicalOp="Nested Loops" EstimateRows="1">'
        '<Warnings NoJoinPredicate="true"/></RelOp>')))
    assert s.signals["no_join_predicate"] == 1
    assert any("join predicate" in w for w in s.warnings)


def test_scalar_udf_detected():
    s = analyze_plan(_cap(_stmt(
        '<RelOp PhysicalOp="Compute Scalar" EstimateRows="1"/>'
        '<UserDefinedFunction FunctionName="[dbo].[fn_slow]"/>')))
    assert s.signals["scalar_udf_count"] == 1
    assert any("fn_slow" in w for w in s.warnings)


def test_sniffed_params_signal_and_confirmed_warning():
    body = (
        '<ParameterList>'
        '<ColumnReference Column="@d" ParameterCompiledValue="\'2020-01-01\'" '
        'ParameterRuntimeValue="\'2024-06-01\'"/>'
        '</ParameterList>'
        '<RelOp PhysicalOp="Index Seek" EstimateRows="10">'
        '<RunTimeInformation><RunTimeCountersPerThread ActualRows="5000"/></RunTimeInformation>'
        '</RelOp>'
    )
    s = analyze_plan(_cap(_stmt(body)))
    assert s.signals["sniffed_params"], "compiled != runtime must surface as a signal"
    assert any("sniffing confirmed" in w for w in s.warnings)


def test_matching_compiled_runtime_not_flagged():
    body = (
        '<ParameterList>'
        '<ColumnReference Column="@d" ParameterCompiledValue="\'x\'" ParameterRuntimeValue="\'x\'"/>'
        '</ParameterList>'
        '<RelOp PhysicalOp="Index Seek" EstimateRows="10"/>'
    )
    s = analyze_plan(_cap(_stmt(body)))
    assert "sniffed_params" not in s.signals


def test_costliest_statement_attribution():
    xml = (f'<ShowPlanXML {_NS}><BatchSequence><Batch><Statements>'
           + _stmt('<RelOp PhysicalOp="Index Seek" EstimateRows="1"/>',
                   text="SELECT cheap", cost="0.1")
           + _stmt('<RelOp PhysicalOp="Index Seek" EstimateRows="1"/>',
                   text="SELECT expensive", cost="9.9")
           + '</Statements></Batch></BatchSequence></ShowPlanXML>')
    s = analyze_plan(PlanCapture(combo=ParamCombo(values={}, label="t"), plan_xml=xml))
    assert s.signals["statement_count"] == 2
    assert s.signals["costliest_statement"]["text"] == "SELECT expensive"
    assert s.signals["costliest_statement"]["cost_fraction"] == 0.99


def test_wait_stats_passed_through():
    s = analyze_plan(_cap(_stmt('<RelOp PhysicalOp="Index Seek" EstimateRows="1"/>'),
                          wait_stats={"PAGEIOLATCH_SH": 120.0}))
    assert s.signals["top_waits_ms"] == {"PAGEIOLATCH_SH": 120.0}


def test_capture_error_scores_zero():
    cap = PlanCapture(combo=ParamCombo(values={}, label="t"), plan_xml="",
                      error="timeout")
    s = analyze_plan(cap)
    assert s.score == 0.0
