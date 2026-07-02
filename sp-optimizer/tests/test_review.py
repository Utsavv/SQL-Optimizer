"""Offline tests for the deterministic T-SQL review rules."""
from scripts.review import _base_type, lint_proc_text
from scripts.models import ProcParam


def _rules(findings):
    return {f.rule for f in findings}


def test_non_sargable_function_on_column():
    sql = "CREATE PROC p @y int AS SELECT * FROM t WHERE YEAR(OrderDate) = @y;"
    f = lint_proc_text(sql, "dbo.p")
    assert "non_sargable_predicate" in _rules(f)


def test_function_on_parameter_is_fine():
    sql = "CREATE PROC p @d datetime AS SELECT 1 FROM t WHERE OrderDate >= DATEADD(day, -1, @d);"
    # DATEADD wraps the PARAMETER, not a column -> no finding
    assert "non_sargable_predicate" not in _rules(lint_proc_text(sql))
    # same when the wrapped-parameter expression sits LEFT of the comparison
    sql = "SELECT 1 FROM t WHERE CONVERT(date, @d) = OrderDate;"
    assert "non_sargable_predicate" not in _rules(lint_proc_text(sql))


def test_leading_wildcard_like():
    sql = "SELECT 1 FROM t WHERE name LIKE '%smith';"
    assert "leading_wildcard_like" in _rules(lint_proc_text(sql))


def test_catch_all_predicate():
    sql = "SELECT 1 FROM t WHERE (@CustomerId IS NULL OR CustomerId = @CustomerId);"
    assert "catch_all_predicate" in _rules(lint_proc_text(sql))


def test_at_at_identity():
    sql = "INSERT INTO t VALUES (1); SELECT @@IDENTITY;"
    assert "at_at_identity" in _rules(lint_proc_text(sql))


def test_not_in_subquery():
    sql = "SELECT 1 FROM a WHERE id NOT IN (SELECT id FROM b);"
    assert "not_in_subquery" in _rules(lint_proc_text(sql))


def test_tran_without_xact_abort():
    sql = "BEGIN TRAN; UPDATE t SET x = 1; COMMIT;"
    assert "tran_without_xact_abort" in _rules(lint_proc_text(sql))
    ok = "SET XACT_ABORT ON; BEGIN TRAN; UPDATE t SET x = 1; COMMIT;"
    assert "tran_without_xact_abort" not in _rules(lint_proc_text(ok))


def test_nolock_and_cursor():
    sql = "DECLARE c CURSOR FOR SELECT 1; SELECT * FROM t WITH (NOLOCK);"
    r = _rules(lint_proc_text(sql))
    assert "nolock_hint" in r and "cursor_use" in r


def test_set_nocount_missing_and_present():
    assert "no_set_nocount" in _rules(lint_proc_text("SELECT 1;"))
    assert "no_set_nocount" not in _rules(lint_proc_text("SET NOCOUNT ON; SELECT 1;"))


def test_sp_prefix():
    assert "sp_prefix" in _rules(lint_proc_text("SELECT 1;", "dbo.sp_GetStuff"))
    assert "sp_prefix" not in _rules(lint_proc_text("SELECT 1;", "dbo.GetStuff"))


def test_table_variable():
    sql = "DECLARE @rows TABLE (id int); SELECT 1;"
    assert "table_variable" in _rules(lint_proc_text(sql))


def test_comments_do_not_trigger():
    sql = """
    SET NOCOUNT ON;
    -- old code: SELECT * FROM t WITH (NOLOCK) WHERE YEAR(d) = 2020
    /* DECLARE c CURSOR FOR SELECT 1; */
    SELECT id FROM t WHERE d >= @from;
    """
    r = _rules(lint_proc_text(sql))
    assert "nolock_hint" not in r
    assert "cursor_use" not in r
    assert "non_sargable_predicate" not in r


def test_findings_ordered_by_severity():
    sql = "DECLARE @t TABLE (i int); SELECT 1 FROM t WHERE YEAR(d) = 1 AND x LIKE '%a';"
    f = lint_proc_text(sql)
    sev = [x.severity for x in f]
    assert sev == sorted(sev, key=lambda s: {"high": 0, "medium": 1, "info": 2}[s])


def test_base_type():
    assert _base_type("nvarchar(50)") == "nvarchar"
    assert _base_type("datetime2") == "datetime2"
    assert _base_type("decimal(18, 2)") == "decimal"
