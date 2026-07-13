"""Discovery tests: type-aware synthesis (Issue 5), combo eligibility marking
(Issues 5/6), sensitive redaction (Issue 10), and the procedure-level
precondition gate (Issues 4/7/9/10/11/12)."""
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
