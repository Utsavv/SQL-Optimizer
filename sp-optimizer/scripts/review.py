"""Deterministic T-SQL code review for the target procedure.

Runs once per optimization run, before the first capture. Two layers:

  1. ``lint_proc_text`` — pure-text rules over the procedure body (comments
     stripped first). Catches the anti-patterns an execution plan can't name:
     non-SARGable predicates, catch-all filters, dirty-read hints, row-by-row
     processing, missing session options. Fully offline-testable.
  2. ``check_param_column_types`` — compares each parameter's declared type to
     the type of the column it filters (via the same param→column mapping
     discovery uses, then sys.columns). An nvarchar parameter against a
     varchar column is the #1 cause of implicit-conversion scans, and the plan
     only shows the symptom.

Findings are advisory: they are written to evidence, rendered in the report,
and fed to the decision step as ``static_review_findings`` so the model can
fix a root cause instead of patching the plan symptom. Nothing here modifies
the database.
"""
from __future__ import annotations

import re
from typing import Optional

from . import discover
from .models import ProcParam, ReviewFinding

# Severity levels, in display order.
SEVERITIES = ("high", "medium", "info")


def _strip_comments(sql: str) -> str:
    """Remove -- line comments and /* */ block comments so commented-out code
    never triggers a finding. Preserves string literals' content well enough
    for pattern matching (a quoted '--' inside a literal is rare in proc
    bodies and only risks a false negative, not a false positive)."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def _snippet(text: str, start: int, end: int, width: int = 70) -> str:
    s = max(0, start - 10)
    frag = " ".join(text[s:end + width].split())
    return frag[:width]


# Functions that, wrapped around a COLUMN in a predicate, defeat index seeks.
_NON_SARGABLE_FUNCS = (
    "YEAR|MONTH|DAY|DATEPART|DATEADD|DATEDIFF|CONVERT|CAST|UPPER|LOWER|"
    "LTRIM|RTRIM|TRIM|SUBSTRING|LEFT|RIGHT|ISNULL|COALESCE|ABS|ROUND"
)

_NON_SARGABLE_RE = re.compile(
    r"\b(?P<fn>" + _NON_SARGABLE_FUNCS + r")\s*\(\s*(?P<args>[^()]*)\)"
    r"\s*(?:=|<>|!=|>=|<=|>|<|\bLIKE\b)",
    re.IGNORECASE,
)


def lint_proc_text(proc_text: str, proc_name: str = "") -> list[ReviewFinding]:
    """Pure-text review rules. Returns findings ordered by severity."""
    findings: list[ReviewFinding] = []
    text = _strip_comments(proc_text or "")
    if not text.strip():
        return findings

    def add(rule: str, severity: str, message: str, snippet: str = ""):
        findings.append(ReviewFinding(rule=rule, severity=severity,
                                      message=message, snippet=snippet))

    # --- SARGability -------------------------------------------------------
    for m in _NON_SARGABLE_RE.finditer(text):
        args = m.group("args").strip()
        # A function over a parameter or constants is fine — only a wrapped
        # COLUMN defeats a seek. Skipping any arg list that references a
        # parameter trades a few false negatives (ISNULL(col, @default)) for
        # zero false positives on CONVERT(date, @p)-style expressions.
        if "@" in args or re.fullmatch(r"[\d.,\s']*", args):
            continue
        add("non_sargable_predicate", "high",
            f"{m.group('fn').upper()}() wraps a column inside a comparison — the "
            f"predicate cannot use an index seek. Rewrite so the column stands "
            f"alone (e.g. move the function to the other side as a range).",
            _snippet(text, m.start(), m.end()))

    for m in re.finditer(r"\bLIKE\s+N?'%", text, re.IGNORECASE):
        add("leading_wildcard_like", "high",
            "LIKE with a leading wildcard scans the whole index — no seek is "
            "possible. Consider full-text search or a reversed persisted column.",
            _snippet(text, m.start(), m.end()))

    # --- plan stability ----------------------------------------------------
    for m in re.finditer(r"@\w+\s+IS\s+NULL\s+OR\b", text, re.IGNORECASE):
        add("catch_all_predicate", "medium",
            "Catch-all pattern (@p IS NULL OR col = @p): one cached plan must "
            "serve every combination of active filters. Consider OPTION "
            "(RECOMPILE) on the statement or parameterized dynamic SQL.",
            _snippet(text, m.start(), m.end()))

    # --- correctness / semantics -------------------------------------------
    for m in re.finditer(r"@@IDENTITY\b", text):
        add("at_at_identity", "high",
            "@@IDENTITY returns the last identity in the SESSION including "
            "triggers — use SCOPE_IDENTITY() (or the OUTPUT clause) instead.",
            _snippet(text, m.start(), m.end()))

    for m in re.finditer(r"\bNOT\s+IN\s*\(\s*SELECT\b", text, re.IGNORECASE):
        add("not_in_subquery", "medium",
            "NOT IN against a subquery returns no rows if the subquery yields "
            "any NULL, and often plans worse than NOT EXISTS. Prefer NOT EXISTS.",
            _snippet(text, m.start(), m.end()))

    if re.search(r"\bBEGIN\s+TRAN(SACTION)?\b", text, re.IGNORECASE) and \
       not re.search(r"\bSET\s+XACT_ABORT\s+ON\b", text, re.IGNORECASE):
        add("tran_without_xact_abort", "high",
            "The procedure opens a transaction without SET XACT_ABORT ON — a "
            "runtime error or client timeout can leave the transaction open "
            "and locks held. Add SET XACT_ABORT ON at the top.")

    # --- concurrency / result correctness -----------------------------------
    for m in re.finditer(r"\bNOLOCK\b", text, re.IGNORECASE):
        add("nolock_hint", "medium",
            "NOLOCK reads uncommitted data and can return duplicate or missing "
            "rows during page splits. Consider READ COMMITTED SNAPSHOT instead.",
            _snippet(text, m.start(), m.end()))

    # --- row-by-row processing ----------------------------------------------
    for m in re.finditer(r"\bDECLARE\s+\w+\s+CURSOR\b", text, re.IGNORECASE):
        add("cursor_use", "medium",
            "Cursor detected — row-by-row processing rarely scales; most cursor "
            "loops can be rewritten as a single set-based statement.",
            _snippet(text, m.start(), m.end()))

    for m in re.finditer(r"\bWHILE\b", text, re.IGNORECASE):
        add("while_loop", "info",
            "WHILE loop detected — fine for retry/batching logic, but verify it "
            "is not per-row processing that could be set-based.",
            _snippet(text, m.start(), m.end()))
        break  # one note is enough

    # --- session options ------------------------------------------------------
    if not re.search(r"\bSET\s+NOCOUNT\s+ON\b", text, re.IGNORECASE):
        add("no_set_nocount", "info",
            "SET NOCOUNT ON is missing — DONE_IN_PROC messages add network "
            "chatter for every statement, which matters in hot OLTP procs.")

    # --- estimation hazards ---------------------------------------------------
    for m in re.finditer(r"\bDECLARE\s+@\w+\s+(?:AS\s+)?TABLE\b", text, re.IGNORECASE):
        add("table_variable", "info",
            "Table variable detected — before SQL Server 2019 the optimizer "
            "assumes 1 row (then 100), which skews joins over large sets. A "
            "temp table gives real statistics.",
            _snippet(text, m.start(), m.end()))

    # --- naming -----------------------------------------------------------------
    short = (proc_name or "").split(".")[-1].strip("[]")
    if short.lower().startswith("sp_"):
        add("sp_prefix", "medium",
            f"Procedure name '{short}' uses the sp_ prefix — SQL Server checks "
            f"master first for sp_% names, costing a lookup and risking a clash "
            f"with a system proc.")

    order = {s: i for i, s in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: order.get(f.severity, len(SEVERITIES)))
    return findings


# ---- parameter vs column type check -----------------------------------------

_COLUMN_TYPE_SQL = """
SELECT t.name AS type_name, c.max_length
FROM sys.columns c
JOIN sys.types t ON c.user_type_id = t.user_type_id
WHERE c.object_id = OBJECT_ID(?) AND c.name = ?;
"""


def _base_type(sql_type: str) -> str:
    return re.split(r"[(\s]", sql_type.strip().lower(), 1)[0]


# Type-family pairs where comparing them implicit-converts the COLUMN side and
# kills the seek (nvarchar param probing a varchar column is the classic one).
_HIGH_RISK_PAIRS = {
    ("nvarchar", "varchar"), ("nchar", "char"),
    ("nvarchar", "char"), ("nchar", "varchar"),
}


def check_param_column_types(
    cursor, proc_name: str, params: list[ProcParam], proc_text: str
) -> list[ReviewFinding]:
    """Flag parameter/column type mismatches on filtered columns.

    Reuses discovery's param→column mapping; every DB read degrades silently —
    a type check must never abort a run."""
    findings: list[ReviewFinding] = []
    if not proc_text:
        return findings
    try:
        aliases = discover._resolve_aliases(proc_text)
    except Exception:
        return findings

    for p in params:
        if p.is_output:
            continue
        try:
            info = discover._column_for_param(proc_text, p.name, aliases)
        except Exception:
            info = None
        if not info:
            continue
        table, col, _op = info
        col_type = _column_type(cursor, table, col)
        if not col_type:
            continue
        p_base, c_base = _base_type(p.sql_type), _base_type(col_type)
        if p_base == c_base:
            continue
        if (p_base, c_base) in _HIGH_RISK_PAIRS:
            findings.append(ReviewFinding(
                rule="param_column_type_mismatch", severity="high",
                message=f"{p.name} is {p.sql_type} but filters {table}.{col} "
                        f"({col_type}): the Unicode parameter forces an implicit "
                        f"conversion of the COLUMN, turning seeks into scans. "
                        f"Match the parameter type to the column.",
                snippet=f"{p.name} -> {table}.{col}"))
        else:
            findings.append(ReviewFinding(
                rule="param_column_type_mismatch", severity="medium",
                message=f"{p.name} is {p.sql_type} but filters {table}.{col} "
                        f"({col_type}): differing types can force implicit "
                        f"conversions and skew cardinality estimates.",
                snippet=f"{p.name} -> {table}.{col}"))
    return findings


def _column_type(cursor, table: str, column: str) -> Optional[str]:
    try:
        cursor.execute(_COLUMN_TYPE_SQL, table, column)
        row = cursor.fetchone()
        return row.type_name if row else None
    except Exception:
        return None


def review_procedure(
    cursor, proc_name: str, proc_text: str, params: list[ProcParam]
) -> list[ReviewFinding]:
    """Full review: text lint + param/column type checks, severity-ordered."""
    findings = lint_proc_text(proc_text, proc_name)
    findings += check_param_column_types(cursor, proc_name, params, proc_text)
    order = {s: i for i, s in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: order.get(f.severity, len(SEVERITIES)))
    return findings
