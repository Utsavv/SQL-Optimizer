"""Discovery tests: type-aware synthesis (Issue 5), combo eligibility marking
(Issues 5/6), sensitive redaction (Issue 10), the procedure-level precondition
gate (Issues 4/7/9/10/11/12), and CASE-expression datetime range-pair mapping."""
from datetime import datetime

import pytest

from scripts import discover, eligibility
from scripts.models import ParamCombo, ProcParam
from mockdb import MockCursor, Row


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in ("SP_OPT_COMBOS", "SP_OPT_SETUP_SQL", "SP_OPT_TEARDOWN_SQL", "SP_OPT_ALLOW_BULK"):
        monkeypatch.delenv(var, raising=False)


# ---- get_signature metadata ------------------------------------------------

def test_signature_parses_tvp_sensitive_and_decimal():
    cur = MockCursor([("sys.parameters", [
        Row(param_name="@Orders", type_name="OrderList", max_length=-1, precision=0,
            scale=0, is_output=0, has_default=0, is_table_type=1),
        Row(param_name="@NewPassword", type_name="nvarchar", max_length=200, precision=0,
            scale=0, is_output=0, has_default=0, is_table_type=0),
        Row(param_name="@Amount", type_name="decimal", max_length=9, precision=5,
            scale=2, is_output=0, has_default=0, is_table_type=0),
    ])])
    params = discover.get_signature(cur, "Website.InsertCustomerOrders")
    by = {p.name: p for p in params}
    assert by["@Orders"].is_table_type
    assert by["@NewPassword"].is_sensitive
    assert by["@Amount"].sql_type == "decimal(5,2)"


# ---- Issue 5: synthesized values are type-aware -----------------------------

def test_synthesize_combos_respects_tinyint_range():
    params = [ProcParam(name="@Flags", sql_type="tinyint")]
    combos = discover.synthesize_combos(params, max_combos=12)
    for c in combos:
        assert eligibility.value_fits_type(c.values["@Flags"], "tinyint")


# ---- Issue 5/6: combo eligibility marking ----------------------------------

def test_mark_combo_invalid_when_value_overflows_type():
    params = [ProcParam(name="@Flags", sql_type="tinyint")]
    combos = [ParamCombo(values={"@Flags": 1000}, label="overflow")]
    discover.mark_combo_eligibility(combos, params)
    assert combos[0].status == eligibility.INVALID_INPUT


def test_mark_combo_principal_collision():
    params = [ProcParam(name="@RoleName", sql_type="sysname"),
              ProcParam(name="@UserName", sql_type="sysname")]
    combos = [ParamCombo(values={"@RoleName": "db_backupoperator",
                                 "@UserName": "db_backupoperator"}, label="typical")]
    discover.mark_combo_eligibility(combos, params)
    assert combos[0].status == eligibility.REQUIRES_CURATED_WORKLOAD


def test_valid_combo_stays_ok():
    params = [ProcParam(name="@Flags", sql_type="tinyint")]
    combos = [ParamCombo(values={"@Flags": 5}, label="ok")]
    discover.mark_combo_eligibility(combos, params)
    assert combos[0].status == eligibility.OK


# ---- Issue 10: sensitive values are redacted, never persisted --------------

def test_sensitive_values_redacted():
    params = [ProcParam(name="@NewPassword", sql_type="nvarchar(200)", is_sensitive=True)]
    combos = [ParamCombo(values={"@NewPassword": "common_value"}, label="x")]
    discover._redact_sensitive_combos(combos, params)
    assert combos[0].values["@NewPassword"] == "***REDACTED***"
    assert "common_value" not in str(combos[0].values)


# ---- proc-level precondition gate ------------------------------------------

def _proc_params(**named):
    return [ProcParam(name=n, sql_type=t, is_table_type=tvp, is_sensitive=sens)
            for n, (t, tvp, sens) in named.items()]


def test_tvp_requires_curated_workload():
    params = _proc_params(**{"@Orders": ("OrderList", True, False)})
    block = discover.assess_proc_eligibility(MockCursor(), "Website.InsertCustomerOrders",
                                             params, "INSERT ... SELECT * FROM @Orders")
    assert block and block[0] == eligibility.REQUIRES_CURATED_WORKLOAD


def test_sensitive_requires_sensitive_input():
    params = _proc_params(**{"@PersonID": ("int", False, False),
                             "@NewPassword": ("nvarchar(200)", False, True)})
    block = discover.assess_proc_eligibility(MockCursor(), "Website.ChangePassword",
                                             params, "UPDATE People SET ...")
    assert block and block[0] == eligibility.REQUIRES_SENSITIVE_INPUT


def test_json_param_requires_curated_workload():
    params = _proc_params(**{"@FullSensorDataArray": ("nvarchar(2000)", False, False)})
    body = "IF ISJSON(@FullSensorDataArray) = 0 RAISERROR('bad',16,1);"
    block = discover.assess_proc_eligibility(MockCursor(), "Website.RecordVehicleTemperature",
                                             params, body)
    assert block and block[0] == eligibility.REQUIRES_CURATED_WORKLOAD


def test_bulk_generator_requires_optin():
    params = _proc_params(**{"@n": ("int", False, False)})
    body = "WHILE @d < GETDATE() BEGIN INSERT INTO t SELECT 1; END"
    block = discover.assess_proc_eligibility(MockCursor(),
                                             "DataLoadSimulation.PopulateDataToCurrentDate",
                                             params, body)
    assert block and block[0] == eligibility.REQUIRES_CURATED_WORKLOAD


def test_bulk_generator_optin_allows_run(monkeypatch):
    monkeypatch.setenv("SP_OPT_ALLOW_BULK", "1")
    params = _proc_params(**{"@n": ("int", False, False)})
    body = "WHILE @d < GETDATE() BEGIN INSERT INTO t SELECT 1; END"
    block = discover.assess_proc_eligibility(MockCursor(),
                                             "DataLoadSimulation.PopulateDataToCurrentDate",
                                             params, body)
    assert block is None


def test_setup_teardown_requires_setup():
    params = _proc_params(**{"@x": ("int", False, False)})
    block = discover.assess_proc_eligibility(
        MockCursor(), "DataLoadSimulation.ReactivateTemporalTablesAfterDataLoad",
        params, "ALTER TABLE ... SET (SYSTEM_VERSIONING = ON)")
    assert block and block[0] == eligibility.REQUIRES_SETUP


def test_full_text_blocked_when_server_missing_component():
    params = _proc_params(**{"@q": ("nvarchar(100)", False, False)})
    cur = MockCursor([("SERVERPROPERTY", [Row(v=0)])])
    block = discover.assess_proc_eligibility(
        cur, "dbo.uspSearchCandidateResumes", params,
        "SELECT * FROM t WHERE CONTAINS(Resume, @q)")
    assert block and block[0] == eligibility.BLOCKED_PREREQUISITE


def test_full_text_ok_when_server_has_component():
    params = _proc_params(**{"@q": ("nvarchar(100)", False, False)})
    cur = MockCursor([("SERVERPROPERTY", [Row(v=1)])])
    block = discover.assess_proc_eligibility(
        cur, "dbo.uspSearchCandidateResumes", params,
        "SELECT * FROM t WHERE CONTAINS(Resume, @q)")
    assert block is None


def test_ordinary_proc_not_blocked():
    params = _proc_params(**{"@id": ("int", False, False)})
    block = discover.assess_proc_eligibility(MockCursor(), "dbo.GetThing", params,
                                             "SELECT * FROM t WHERE id = @id")
    assert block is None


# ---- CASE-expression datetime range-pair mapping ---------------------------
#
# WWI's Integration.GetOrderUpdates / GetSaleUpdates bound both cutoff params
# against a max-of-two-datetimes expression:
#     CASE WHEN ol.LastEditedWhen > o.LastEditedWhen
#          THEN ol.LastEditedWhen ELSE o.LastEditedWhen END > @LastCutoff
#      AND CASE WHEN ... END <= @NewCutoff
# The simple matcher used to capture the CASE keyword END as the "column",
# fail to resolve it, and fall through to a Cartesian synthesis that produced
# an inverted (lower > upper) window and all-future/empty ranges.

_CASE_PROC = """
CREATE PROCEDURE Integration.GetOrderUpdates
    @LastCutoff datetime2(7), @NewCutoff datetime2(7)
AS
BEGIN
    SELECT o.OrderID, ol.OrderLineID
    FROM Sales.Orders AS o
    INNER JOIN Sales.OrderLines AS ol ON o.OrderID = ol.OrderID
    WHERE CASE WHEN ol.LastEditedWhen > o.LastEditedWhen THEN ol.LastEditedWhen ELSE o.LastEditedWhen END > @LastCutoff
      AND CASE WHEN ol.LastEditedWhen > o.LastEditedWhen THEN ol.LastEditedWhen ELSE o.LastEditedWhen END <= @NewCutoff;
END
"""

_SIMPLE_PROC = """
CREATE PROCEDURE Integration.GetMovementUpdates
    @LastCutoff datetime2(7), @NewCutoff datetime2(7)
AS
BEGIN
    SELECT sit.StockItemTransactionID
    FROM Warehouse.StockItemTransactions AS sit
    WHERE sit.LastEditedWhen > @LastCutoff AND sit.LastEditedWhen <= @NewCutoff;
END
"""

_RANGE_PARAMS = [ProcParam(name="@LastCutoff", sql_type="datetime2"),
                 ProcParam(name="@NewCutoff", sql_type="datetime2")]


def _bounds(combos):
    return [(datetime.strptime(c.values["@LastCutoff"], "%Y-%m-%d %H:%M:%S"),
             datetime.strptime(c.values["@NewCutoff"], "%Y-%m-%d %H:%M:%S"))
            for c in combos]


def test_case_expr_maps_both_bounds_to_real_column():
    aliases = discover._resolve_aliases(_CASE_PROC)
    lo = discover._column_for_param(_CASE_PROC, "@LastCutoff", aliases)
    hi = discover._column_for_param(_CASE_PROC, "@NewCutoff", aliases)
    assert lo == ("Sales.OrderLines", "LastEditedWhen", ">")
    assert hi == ("Sales.OrderLines", "LastEditedWhen", "<=")


def test_simple_matcher_rejects_case_end_keyword():
    # The literal CASE keyword END must never be mistaken for a column.
    aliases = discover._resolve_aliases(_CASE_PROC)
    assert discover._simple_column_for_param(_CASE_PROC, "@LastCutoff", aliases) is None


@pytest.mark.parametrize("proc", [_CASE_PROC, _SIMPLE_PROC])
def test_range_pair_windows_are_ordered_and_anchored(proc):
    # min/max come back as real data; every window must be ordered and no later
    # than the real max (i.e. not an all-future synthetic spread).
    data_hi = datetime(2016, 5, 31, 12, 0, 0)
    cur = MockCursor([("MIN(", [Row(lo=datetime(2013, 1, 1), hi=data_hi)])])
    combos = discover.derive_combos_from_data(cur, _RANGE_PARAMS, proc, max_combos=12)
    assert combos
    bounds = _bounds(combos)
    assert all(lo <= hi for lo, hi in bounds), "inverted window produced"
    assert any(hi <= data_hi for _, hi in bounds), "no window anchored within real data"


def test_synth_fallback_range_pair_is_ordered_never_inverted():
    # No proc text mapping possible from the DB, but the fallback still orders
    # the pair from the operators instead of emitting a Cartesian product.
    combos = discover.synthesize_combos(_RANGE_PARAMS, max_combos=12, proc_text=_CASE_PROC)
    bounds = _bounds(combos)
    assert bounds and all(lo <= hi for lo, hi in bounds)
    # narrow/medium/wide/empty shape, not the old 2x2 Cartesian product
    assert len(combos) == 4


def test_range_pair_synth_when_minmax_unreadable():
    # Mapping succeeds but the column min/max can't be read -> ordered synthetic
    # windows, still never inverted (regression guard for the old `return []`).
    cur = MockCursor()  # no MIN rule -> _column_min_max returns None
    combos = discover.derive_combos_from_data(cur, _RANGE_PARAMS, _CASE_PROC, max_combos=12)
    bounds = _bounds(combos)
    assert bounds and all(lo <= hi for lo, hi in bounds)
