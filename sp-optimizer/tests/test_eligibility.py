"""Unit tests for the generic eligibility / value-validity classifier.

Covers the type-aware numeric ranges (Issue 5), sensitive-parameter detection
(Issue 10), TVP / structured-format / full-text / bulk-generator detection
(Issues 11, 12, 4, 7), setup/teardown pairing (Issue 9), cross-parameter and
special-principal validity (Issue 6), and SQL-error classification (Issues
3/4/5/6/9/11/12).
"""
from decimal import Decimal

import pytest

from scripts import eligibility as e


# ---- Issue 5: type-aware numeric ranges + validation -----------------------

@pytest.mark.parametrize("sql_type,lo,hi", [
    ("tinyint", 0, 255),
    ("smallint", -32768, 32767),
    ("int", -2147483648, 2147483647),
    ("bigint", -9223372036854775808, 9223372036854775807),
    ("bit", 0, 1),
])
def test_integer_bounds(sql_type, lo, hi):
    assert e.numeric_bounds(sql_type) == (lo, hi)


def test_decimal_bounds_from_precision_scale():
    assert e.numeric_bounds("decimal(5,2)") == (Decimal("-999.99"), Decimal("999.99"))
    assert e.numeric_bounds("numeric(3)") == (Decimal("-999"), Decimal("999"))


def test_non_numeric_bounds_is_none():
    assert e.numeric_bounds("nvarchar(50)") is None
    assert e.numeric_bounds("datetime2") is None


@pytest.mark.parametrize("sql_type", ["tinyint", "smallint", "int", "bigint", "decimal(5,2)", "bit"])
def test_synth_values_all_fit_declared_type(sql_type):
    """No synthesized numeric value may overflow / fail conversion (Issue 5)."""
    for v in e.numeric_synth_values(sql_type):
        assert e.value_fits_type(v, sql_type), f"{v!r} does not fit {sql_type}"


def test_tinyint_never_overflows():
    vals = e.numeric_synth_values("tinyint")
    assert max(vals) <= 255 and min(vals) >= 0
    assert 1000 not in vals and 999999 not in vals


def test_value_fits_type_rejects_overflow_and_bad_conversions():
    assert not e.value_fits_type(1000, "tinyint")
    assert not e.value_fits_type(999999, "tinyint")
    assert e.value_fits_type(200, "tinyint")
    assert not e.value_fits_type(1.5, "int")        # non-integral into int
    assert e.value_fits_type(1.5, "decimal(5,2)")
    assert not e.value_fits_type("abcdef", "varchar(3)")  # length overflow
    assert e.value_fits_type("abc", "varchar(3)")
    assert e.value_fits_type(None, "tinyint")       # NULL always fits


# ---- Issue 10: sensitive-parameter detection -------------------------------

@pytest.mark.parametrize("name", [
    "@NewPassword", "@OldPassword", "@pwd", "@ApiToken", "@SecretKey",
    "@access_key", "@Credential", "@passphrase",
])
def test_sensitive_params_detected(name):
    assert e.is_sensitive_param(name)


@pytest.mark.parametrize("name", ["@PersonID", "@CustomerName", "@OrderDate", "@Quantity"])
def test_non_sensitive_params(name):
    assert not e.is_sensitive_param(name)


def test_redaction_never_returns_value():
    assert e.redact("hunter2") == "***REDACTED***"
    assert "hunter2" not in e.redact("hunter2")


# ---- Issue 12: structured JSON / XML formats -------------------------------

def test_json_param_detected():
    body = "IF ISJSON(@FullSensorDataArray) = 0 RAISERROR('bad', 16, 1);"
    assert e.param_expects_json(body, "@FullSensorDataArray")


def test_openjson_param_detected():
    body = "INSERT INTO t SELECT * FROM OPENJSON(@Payload)"
    assert e.param_expects_json(body, "@Payload")


def test_non_json_param_not_flagged():
    body = "SELECT * FROM t WHERE Name = @Name"
    assert not e.param_expects_json(body, "@Name")


def test_xml_param_detected_by_type_and_method():
    assert e.param_expects_xml("", "@Doc", "xml")
    assert e.param_expects_xml("SELECT @Doc.value('(/a)[1]', 'int')", "@Doc", "nvarchar(max)")


# ---- Issue 4: Full-Text prerequisite ---------------------------------------

@pytest.mark.parametrize("body", [
    "SELECT * FROM t WHERE CONTAINS(col, @q)",
    "SELECT * FROM FREETEXTTABLE(t, col, @q)",
    "SELECT * FROM t WHERE FREETEXT(col, @q)",
    "SELECT * FROM CONTAINSTABLE(t, col, @q)",
])
def test_full_text_predicates_detected(body):
    assert e.requires_full_text(body)


def test_no_full_text():
    assert not e.requires_full_text("SELECT * FROM t WHERE col = @q")


# ---- Issue 7: unbounded bulk-generator detection ---------------------------

def test_bulk_loop_to_current_date():
    body = "WHILE @d < GETDATE() BEGIN INSERT INTO t SELECT 1; SET @d = DATEADD(day,1,@d); END"
    assert e.is_bulk_generator(body, "DataLoadSimulation.PopulateDataToCurrentDate")


def test_bulk_name_hint():
    assert e.is_bulk_generator("SELECT 1", "DataLoadSimulation.PopulateDataToCurrentDate")


def test_non_bulk_proc():
    assert e.is_bulk_generator("SELECT * FROM t WHERE id = @id", "dbo.GetThing") is None


# ---- Issue 9: setup/teardown pairing ---------------------------------------

def test_setup_partner_reactivate_after():
    assert (e.setup_partner("DataLoadSimulation.ReactivateTemporalTablesAfterDataLoad")
            == "DataLoadSimulation.DeactivateTemporalTablesBeforeDataLoad")


def test_setup_partner_none_for_ordinary_proc():
    assert e.setup_partner("dbo.uspGetBillOfMaterials") is None
    assert e.setup_partner("Website.ChangePassword") is None


# ---- Issue 6: cross-parameter / special-principal validity -----------------

def test_role_user_collision_rejected():
    reason = e.validate_principal_combo(
        {"@RoleName": "db_backupoperator", "@UserName": "db_backupoperator"})
    assert reason and "same principal" in reason


def test_special_principal_as_user_rejected():
    reason = e.validate_principal_combo({"@RoleName": "Sales", "@UserName": "db_owner"})
    assert reason and "15405" in reason


def test_valid_role_user_pair_ok():
    assert e.validate_principal_combo({"@RoleName": "Sales", "@UserName": "alice"}) is None


def test_no_principal_params_ok():
    assert e.validate_principal_combo({"@OrderID": 5, "@Qty": 2}) is None


def test_is_special_principal():
    assert e.is_special_principal("db_backupoperator")
    assert e.is_special_principal("PUBLIC")
    assert e.is_special_principal("sys")
    assert not e.is_special_principal("alice")


# ---- Issues 3/4/5/6/9/11/12: SQL-error classification ----------------------

@pytest.mark.parametrize("error,status", [
    ("Full-Text Search is not installed (error 7609)", e.BLOCKED_PREREQUISITE),
    ("Error converting data type int to tinyint.", e.INVALID_INPUT),
    ("Cannot use the special principal 'db_backupoperator'.", e.REQUIRES_CURATED_WORKLOAD),
    ("Temporal SYSTEM_TIME period is already defined", e.REQUIRES_SETUP),
    ("Operand type clash: NULL is incompatible with OrderList", e.REQUIRES_CURATED_WORKLOAD),
    ("Query timeout expired", e.TIMEOUT),
])
def test_classify_sql_error(error, status):
    result = e.classify_sql_error(error)
    assert result is not None and result[0] == status


def test_classify_unknown_error_is_none():
    assert e.classify_sql_error("Some novel unrecognized failure") is None
    assert e.classify_sql_error(None) is None
    assert e.classify_sql_error("") is None
