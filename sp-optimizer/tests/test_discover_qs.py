"""Offline tests for Query Store combo mining."""
from scripts.discover import _clean_compiled_value, combos_from_query_store
from scripts.models import ProcParam

_NS = 'xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan"'


def _plan_xml(param_values: dict[str, str]) -> str:
    cols = "".join(
        f'<ColumnReference Column="{name}" ParameterCompiledValue="{val}"/>'
        for name, val in param_values.items()
    )
    return (f'<ShowPlanXML {_NS}><BatchSequence><Batch><Statements>'
            f'<StmtSimple><QueryPlan><ParameterList>{cols}</ParameterList>'
            f'</QueryPlan></StmtSimple></Statements></Batch></BatchSequence></ShowPlanXML>')


class QSFakeCursor:
    def __init__(self, plans):
        self.plans = plans

    def execute(self, sql, *params):
        pass

    def fetchall(self):
        return [(p,) for p in self.plans]


def test_clean_compiled_value():
    assert _clean_compiled_value("(42)") == 42
    assert _clean_compiled_value("((1.5))") == 1.5
    assert _clean_compiled_value("N'BRG'") == "BRG"
    assert _clean_compiled_value("'it''s'") == "it's"
    assert _clean_compiled_value("NULL") is None
    assert _clean_compiled_value("('2024-06-01 00:00:00')") == "2024-06-01 00:00:00"
    assert _clean_compiled_value(None) is None


def _params():
    return [ProcParam(name="@From", sql_type="datetime"),
            ProcParam(name="@To", sql_type="datetime")]


def test_combos_mined_from_plans():
    plans = [
        _plan_xml({"@From": "'2024-01-01'", "@To": "'2024-01-02'"}),
        _plan_xml({"@From": "'2020-01-01'", "@To": "'2024-06-01'"}),
    ]
    combos = combos_from_query_store(QSFakeCursor(plans), "dbo.p", _params())
    assert len(combos) == 2
    assert combos[0].values == {"@From": "2024-01-01", "@To": "2024-01-02"}
    assert combos[0].weight == 2.0
    assert "query store" in combos[0].label


def test_duplicate_plans_deduped():
    plan = _plan_xml({"@From": "'a'", "@To": "'b'"})
    combos = combos_from_query_store(QSFakeCursor([plan, plan]), "dbo.p", _params())
    assert len(combos) == 1


def test_incomplete_parameter_coverage_skipped():
    # plan only compiled @From; @To has no default -> can't build a full EXEC
    plans = [_plan_xml({"@From": "'2024-01-01'"})]
    combos = combos_from_query_store(QSFakeCursor(plans), "dbo.p", _params())
    assert combos == []


def test_default_params_may_be_missing():
    params = [ProcParam(name="@From", sql_type="datetime"),
              ProcParam(name="@Top", sql_type="int", has_default=True)]
    plans = [_plan_xml({"@From": "'2024-01-01'"})]
    combos = combos_from_query_store(QSFakeCursor(plans), "dbo.p", params)
    assert len(combos) == 1


def test_query_store_unavailable_returns_empty():
    class Exploding:
        def execute(self, *a):
            raise RuntimeError("Query Store is OFF")

    assert combos_from_query_store(Exploding(), "dbo.p", _params()) == []
