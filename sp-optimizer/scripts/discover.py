"""Step 1: discover the parameter space of a stored procedure.

Strategy (in priority order):
  1. Read the proc signature from sys.parameters to learn names/types.
  2. If SP_OPT_COMBOS points at a JSON file, use those combos verbatim (lets a
     caller inject a fully hand-curated workload for any proc).
  3. Otherwise, DERIVE a realistic workload from the proc's *own* data. For each
     parameter, map it to the table column it filters and mine that column's
     actual contents:
       - datetime range/bound filters -> real min/max, fanned into narrow ->
         wide -> empty windows (the classic sniffing spread).
       - equality/other filters -> real frequency-ranked values (a hot common
         value AND a selective rare one), so the proc is exercised with argument
         values that actually exist in the data instead of invented constants.
     On top of that, any OPTIONAL / catch-all parameter (declared with a default,
     or guarded by ``@p IS NULL OR ...`` / ISNULL / COALESCE) is enumerated on
     the NULL vs. real-value axis, so every combination of "which optional
     filters are supplied" is represented — each combination can compile to a
     very different plan. This is what makes the skill generic across procedures:
     the workload is computed from whatever proc you point it at.
  4. As a final fallback (no DB introspection possible), synthesize boundary +
     typical values per type so a workload always exists.

This module is deliberately conservative: it returns candidate combos but
never executes anything destructive. Every DB-facing step degrades gracefully
to the next strategy on any error, so a quirk in one proc never aborts the run.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from itertools import combinations, product
from typing import Optional

from .models import ParamCombo, ProcParam


def _combos_from_env() -> Optional[list[ParamCombo]]:
    """If SP_OPT_COMBOS points at a JSON file, use those combos verbatim.

    Lets the caller inject a realistic workload (e.g. cutoff dates mined from the
    actual fact table) instead of the deterministic boundary synthesis, which can
    fall outside a column's real value range. File shape:
        [{"values": {"@LastCutoff": "...", "@NewCutoff": "..."},
          "label": "...", "weight": 1.0}, ...]
    """
    path = os.environ.get("SP_OPT_COMBOS")
    if not path or not os.path.exists(path):
        return None
    with open(path) as f:
        raw = json.load(f)
    combos = [
        ParamCombo(values=c["values"], label=c.get("label", ""), weight=float(c.get("weight", 1.0)))
        for c in raw
    ]
    return combos or None

# ---- 1. signature -----------------------------------------------------------

SIGNATURE_SQL = """
SELECT p.name              AS param_name,
       t.name              AS type_name,
       p.max_length        AS max_length,
       p.is_output         AS is_output,
       p.has_default_value AS has_default,
       p.default_value     AS default_value
FROM sys.parameters p
JOIN sys.types t ON p.user_type_id = t.user_type_id
WHERE p.object_id = OBJECT_ID(?)
ORDER BY p.parameter_id;
"""


def get_signature(cursor, proc_name: str) -> list[ProcParam]:
    cursor.execute(SIGNATURE_SQL, proc_name)
    params: list[ProcParam] = []
    for row in cursor.fetchall():
        type_disp = row.type_name
        if row.type_name in ("varchar", "nvarchar", "char", "nchar"):
            type_disp = f"{row.type_name}({row.max_length})"
        has_default = bool(getattr(row, "has_default", False))
        default_val = getattr(row, "default_value", None)
        params.append(
            ProcParam(
                name=row.param_name,
                sql_type=type_disp,
                is_output=bool(row.is_output),
                has_default=has_default,
                default=None if default_val is None else str(default_val),
            )
        )
    return params


def get_proc_text(cursor, proc_name: str) -> str:
    """Return the CREATE/ALTER body of the proc (used to map params -> columns)."""
    try:
        cursor.execute("SELECT OBJECT_DEFINITION(OBJECT_ID(?));", proc_name)
        row = cursor.fetchone()
        return row[0] if row and row[0] else ""
    except Exception:
        return ""


# ---- 2. real values from Query Store ---------------------------------------

QUERY_STORE_VALUES_SQL = """
SELECT TOP (?) qt.query_sql_text
FROM sys.query_store_query q
JOIN sys.query_store_query_text qt ON q.query_text_id = qt.query_text_id
JOIN sys.query_store_plan p ON q.query_id = p.query_id
WHERE q.object_id = OBJECT_ID(?)
ORDER BY q.last_execution_time DESC;
"""


def values_from_query_store(cursor, proc_name: str, limit: int = 50) -> list[str]:
    """Return recent SQL texts that invoked this proc (for value mining).

    Parsing concrete argument values out of these texts is left to the caller /
    LLM step, since the shape varies. Returns [] if Query Store is off.
    """
    try:
        cursor.execute(QUERY_STORE_VALUES_SQL, limit, proc_name)
        return [r.query_sql_text for r in cursor.fetchall()]
    except Exception:
        return []


# ---- 2b. derive a realistic workload from the proc's own data ---------------
#
# This is the generic engine: for ANY proc, map its parameters to the columns
# they filter, read those columns' real value ranges from the database, and
# build a workload (narrow -> medium -> wide -> empty) anchored to real data.
# This replaces the per-proc hand-written combos.json for the common case.

_DATETIME_TYPES = ("datetimeoffset", "datetime2", "smalldatetime", "datetime", "date")

# Reserved words that can directly follow a table reference in FROM/JOIN; if we
# see one of these where an alias would be, the table has no alias.
_NOT_AN_ALIAS = {
    "on", "where", "inner", "left", "right", "full", "cross", "outer", "join",
    "group", "order", "having", "union", "with", "as", "option", "for", "and", "or",
}


def _is_datetime(param: ProcParam) -> bool:
    t = param.sql_type.lower()
    return any(t.startswith(x) for x in _DATETIME_TYPES)


def _resolve_aliases(proc_text: str) -> dict[str, str]:
    """Map every table alias (and bare table name) -> its source table name.

    Parses FROM/JOIN clauses so a predicate like ``sit.LastEditedWhen`` can be
    traced back to the real table ``Warehouse.StockItemTransactions``.
    """
    aliases: dict[str, str] = {}
    pattern = re.compile(
        r"\b(?:FROM|JOIN)\s+"
        r"(?P<tbl>(?:\[[^\]]+\]|\w+)(?:\s*\.\s*(?:\[[^\]]+\]|\w+)){0,2})"
        r"(?:\s+(?:AS\s+)?(?P<alias>\w+))?",
        re.IGNORECASE,
    )
    for m in pattern.finditer(proc_text):
        tbl = re.sub(r"\s+", "", m.group("tbl"))
        alias = m.group("alias")
        if alias and alias.lower() not in _NOT_AN_ALIAS:
            aliases[alias.lower()] = tbl
        # the table's own short name is always usable as a qualifier too
        bare = tbl.split(".")[-1].strip("[]")
        aliases.setdefault(bare.lower(), tbl)
    return aliases


def _column_for_param(proc_text: str, param_name: str, aliases: dict[str, str]) -> Optional[tuple[str, str, str]]:
    """Best-effort: find the (table, column, operator) a parameter is compared to.

    Handles both orderings:  ``col <op> @param``  and  ``@param <op> col``.
    Returns the table fully resolved through the alias map, or None if the
    parameter isn't a simple column comparison (e.g. wrapped in a function).
    """
    esc = re.escape(param_name)
    col_ref = r"(?:(?P<alias>\w+)\s*\.\s*)?(?P<col>\w+)"
    # col <op> @param
    m = re.search(col_ref + r"\s*(?P<op>>=|<=|<>|>|<|=)\s*" + esc + r"\b",
                  proc_text, re.IGNORECASE)
    if not m:
        # @param <op> col  -> normalise the operator direction
        m = re.search(esc + r"\s*(?P<op>>=|<=|<>|>|<|=)\s*" + col_ref,
                      proc_text, re.IGNORECASE)
        if m:
            flip = {">": "<", "<": ">", ">=": "<=", "<=": ">="}
            op = flip.get(m.group("op"), m.group("op"))
            m_op = op
        else:
            return None
    else:
        m_op = m.group("op")
    alias = (m.group("alias") or "").lower()
    col = m.group("col")
    table = aliases.get(alias) if alias else None
    if table is None:
        # unqualified column: only safe if there is exactly one table in scope
        distinct = set(aliases.values())
        if len(distinct) == 1:
            table = next(iter(distinct))
        else:
            return None
    return table, col, m_op


def _column_min_max(cursor, table: str, column: str) -> Optional[tuple]:
    """Return (min, max) of a column, or None if it can't be read."""
    try:
        cursor.execute(f"SELECT MIN([{column}]) AS lo, MAX([{column}]) AS hi FROM {table};")
        row = cursor.fetchone()
        if row and row.lo is not None and row.hi is not None:
            return row.lo, row.hi
    except Exception:
        return None
    return None


def _sample_column_values(cursor, table: str, column: str) -> tuple[object, object]:
    """Return (most_common_value, rare_value) that actually occur in a column.

    The two ends of the frequency distribution are the values worth testing: the
    hot value the proc is most often called with (large result → the plan the
    optimizer should be good at) and a selective rare value (small result → the
    plan that benefits from a seek). Both are *real* — pulled from the data — so
    an equality predicate like ``col = @p`` is never exercised with an invented
    constant that returns nothing. Returns (None, None) on any error / empty
    column so callers degrade to synthesis.
    """
    try:
        cursor.execute(
            f"SELECT TOP (1) [{column}] AS v FROM {table} "
            f"WHERE [{column}] IS NOT NULL "
            f"GROUP BY [{column}] ORDER BY COUNT_BIG(*) DESC;"
        )
        row = cursor.fetchone()
        common = row.v if row else None
        cursor.execute(
            f"SELECT TOP (1) [{column}] AS v FROM {table} "
            f"WHERE [{column}] IS NOT NULL "
            f"GROUP BY [{column}] ORDER BY COUNT_BIG(*) ASC;"
        )
        row = cursor.fetchone()
        rare = row.v if row else None
        return common, rare
    except Exception:
        return None, None


# Constructs that mark a parameter as OPTIONAL / catch-all, i.e. one whose NULL
# branch short-circuits its filter. Each of these compiled with the param NULL
# vs. supplied can produce a wildly different plan, so both must be exercised.
def _nullable_params(inputs: list[ProcParam], proc_text: str) -> set[str]:
    """Names of params that behave as optional filters (a real NULL branch).

    A param counts as nullable/optional when EITHER it declares a default (the
    caller can omit it), OR the body guards it with the catch-all idioms
    ``@p IS NULL`` / ``ISNULL(@p, ...)`` / ``COALESCE(@p, ...)``. Those are the
    params whose NULL vs. supplied combinations we fan out over.
    """
    nullable: set[str] = set()
    text = proc_text or ""
    for p in inputs:
        esc = re.escape(p.name)
        if p.has_default:
            nullable.add(p.name)
            continue
        if re.search(esc + r"\s+IS\s+NULL", text, re.IGNORECASE):
            nullable.add(p.name)
        elif re.search(r"\bISNULL\s*\(\s*" + esc + r"\b", text, re.IGNORECASE):
            nullable.add(p.name)
        elif re.search(r"\bCOALESCE\s*\([^)]*" + esc + r"\b", text, re.IGNORECASE):
            nullable.add(p.name)
    return nullable


def _fmt_dt(value) -> str:
    """Format a datetime-ish value as an ISO string the EXEC builder can quote."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _coerce_value(value) -> object:
    """Make a DB-sampled value safe for the EXEC arg builder.

    Datetimes are stringified (so they get quoted); everything else (ints,
    Decimals, bits, strings) passes through unchanged and the builder renders it
    correctly by type.
    """
    if isinstance(value, datetime):
        return _fmt_dt(value)
    return value


def _datetime_range_windows(lo, hi) -> list[tuple[object, object, str, float]]:
    """Build (lower, upper, label, weight) windows from a real [lo, hi] range.

    The windows fan from the narrow incremental pulls that dominate real ETL
    traffic out to a full-history reload and an empty window — exactly the
    spread that exposes parameter-sniffing skew. Narrow pulls are weighted
    higher because they are the common case worth optimising for.
    """
    if not isinstance(hi, datetime):
        # Non-datetime (e.g. plain date as string) — keep a simple full/empty pair.
        return [(lo, hi, "full range", 1.0)]
    windows: list[tuple[object, object, str, float]] = []
    for days, label, weight in (
        (1, "narrow: last 1 day", 3.0),
        (7, "narrow: last 7 days", 3.0),
        (30, "medium: last 30 days", 2.0),
        (90, "medium: last 90 days", 2.0),
    ):
        start = hi - timedelta(days=days)
        if start > lo:
            windows.append((start, hi, label, weight))
    windows.append((lo, hi, "wide: full history", 1.0))
    windows.append((hi, hi + timedelta(hours=1), "edge: empty window", 1.0))
    return windows


def _datetime_windows_for(
    cursor,
    inputs: list[ProcParam],
    mapped: dict[str, tuple[str, str, str]],
) -> list[tuple[dict[str, object], str, float]]:
    """Build the datetime axis: a list of (assignment, label, weight).

    Each assignment sets only the datetime param(s) — the caller merges it with
    the rest of the parameter values. Handles the two dominant shapes: a coupled
    lower/upper range pair on one column, and a single datetime bound. Returns []
    when no datetime param maps to a readable column (there is simply no date
    axis to vary).
    """
    dt_inputs = [p for p in inputs if _is_datetime(p) and p.name in mapped]
    if not dt_inputs:
        return []
    dmapped = {p.name: mapped[p.name] for p in dt_inputs}

    # Detect a lower/upper range pair sharing the same table+column.
    lowers = {n: i for n, i in dmapped.items() if i[2] in (">", ">=")}
    uppers = {n: i for n, i in dmapped.items() if i[2] in ("<", "<=")}
    range_pair = None
    for ln, li in lowers.items():
        for un, ui in uppers.items():
            if ln != un and (li[0], li[1]) == (ui[0], ui[1]):
                range_pair = (ln, un, li[0], li[1])  # lower, upper, table, col
                break
        if range_pair:
            break

    out: list[tuple[dict[str, object], str, float]] = []
    if range_pair:
        lparam, uparam, table, col = range_pair
        rng = _column_min_max(cursor, table, col)
        if not rng:
            return []
        lo, hi = rng
        for lower, upper, label, weight in _datetime_range_windows(lo, hi):
            out.append(({lparam: _fmt_dt(lower), uparam: _fmt_dt(upper)}, label, weight))
    else:
        # Single datetime bound (e.g. col >= @FromDate): vary that one param.
        pname, (table, col, _op) = next(iter(dmapped.items()))
        rng = _column_min_max(cursor, table, col)
        if not rng:
            return []
        lo, hi = rng
        if isinstance(hi, datetime):
            for days, label, weight in (
                (1, "narrow: last 1 day", 3.0),
                (7, "narrow: last 7 days", 3.0),
                (30, "medium: last 30 days", 2.0),
            ):
                start = hi - timedelta(days=days)
                if start > lo:
                    out.append(({pname: _fmt_dt(start)}, label, weight))
            out.append(({pname: _fmt_dt(lo)}, "wide: full history", 1.0))
            out.append(({pname: _fmt_dt(hi + timedelta(hours=1))}, "edge: future/empty", 1.0))
        else:
            out.append(({pname: _fmt_dt(lo)}, "min", 1.0))
            out.append(({pname: _fmt_dt(hi)}, "max", 1.0))
    return out


def derive_combos_from_data(
    cursor,
    params: list[ProcParam],
    proc_text: str,
    max_combos: int = 12,
) -> Optional[list[ParamCombo]]:
    """Derive a data-anchored workload for any proc, or None if not possible.

    Two axes of variation, both mined from the proc's own tables:

    1. **Datetime windows** — the classic sniffing spread (narrow → wide → empty)
       built from a date column's real min/max.
    2. **Optional-parameter NULL combinations** — for every catch-all / optional
       param, both its NULL branch and a real supplied value are exercised, and
       the *combinations* across those params are enumerated (each combination
       is a different set of active filters → potentially a different plan).

    Non-nullable equality params are pinned to a real, frequently-occurring
    value (with a selective rare value tried as an extra variant). Returns None —
    so the caller falls back to type-based synthesis — only when nothing about
    the proc can be anchored to real data (no mappable column, no optional param).
    """
    inputs = [p for p in params if not p.is_output]
    if not inputs or not proc_text:
        return None

    aliases = _resolve_aliases(proc_text)

    # Map every input param -> (table, column, op), best effort, any type/op.
    mapped: dict[str, tuple[str, str, str]] = {}
    for p in inputs:
        info = _column_for_param(proc_text, p.name, aliases)
        if info:
            mapped[p.name] = info

    nullable = _nullable_params(inputs, proc_text)

    # Nothing to anchor to real data and no optional axis to explore → let the
    # type-based synthesizer take over.
    if not mapped and not nullable:
        return None

    by_name = {p.name: p for p in inputs}

    # Real representative value per param. Datetime params vary via the window
    # sweep, so they are excluded here; every other param gets a hot common value
    # (typical) and, when available, a selective rare value.
    typical: dict[str, object] = {}
    selective: dict[str, object] = {}
    for p in inputs:
        if _is_datetime(p):
            continue
        common = rare = None
        if p.name in mapped:
            table, col, _op = mapped[p.name]
            common, rare = _sample_column_values(cursor, table, col)
        if common is None:
            syn = _synth_values(p)
            common = syn[0] if syn else None
        typical[p.name] = _coerce_value(common)
        if rare is not None:
            selective[p.name] = _coerce_value(rare)

    dt_windows = _datetime_windows_for(cursor, inputs, mapped)

    # Any datetime param NOT covered by the window sweep still needs a value so
    # the EXEC call is complete; fall back to a synth constant for it.
    covered_dt: set[str] = set()
    for assign, _, _ in dt_windows:
        covered_dt |= set(assign)
    for p in inputs:
        if _is_datetime(p) and p.name not in covered_dt and p.name not in typical:
            syn = _synth_values(p)
            typical[p.name] = syn[0] if syn else None

    def base_assignment() -> dict[str, object]:
        return dict(typical)

    # A representative mid window to hold the date axis fixed while NULL combos
    # vary the optional filters (so the two axes don't multiply out of control).
    dt_mid = dt_windows[len(dt_windows) // 2][0] if dt_windows else {}

    # --- assemble combos in priority order --------------------------------
    # Ordered so the highest-signal combos survive the max_combos cap:
    #   baseline (all real) → each optional param NULL on its own →
    #   the rest of the date sweep → multi-param NULL combinations →
    #   selective rare-value variants.
    baseline: list[ParamCombo] = []
    single_null: list[ParamCombo] = []
    date_sweep: list[ParamCombo] = []
    multi_null: list[ParamCombo] = []
    selective_combos: list[ParamCombo] = []

    if dt_windows:
        first_assign, first_label, first_weight = dt_windows[0]
        vals = base_assignment()
        vals.update(first_assign)
        baseline.append(ParamCombo(values=vals, label=first_label, weight=first_weight))
        for assign, label, weight in dt_windows[1:]:
            vals = base_assignment()
            vals.update(assign)
            date_sweep.append(ParamCombo(values=vals, label=label, weight=weight))
    else:
        baseline.append(
            ParamCombo(values=base_assignment(), label="typical: real common values", weight=2.0)
        )

    # Preserve signature order (nullable is a set) so combo labels and the
    # order combos are generated in are stable run-to-run.
    nullable_list = [p.name for p in inputs if p.name in nullable]
    for r in range(1, len(nullable_list) + 1):
        for subset in combinations(nullable_list, r):
            vals = base_assignment()
            vals.update(dt_mid)
            for n in subset:
                vals[n] = None
            names = ", ".join(subset)
            combo = ParamCombo(
                values=vals,
                label=f"optional NULL: {names}",
                weight=1.5 if r == 1 else 1.0,
            )
            (single_null if r == 1 else multi_null).append(combo)

    for name, rare in selective.items():
        vals = base_assignment()
        if dt_windows:
            vals.update(dt_windows[0][0])
        vals[name] = rare
        selective_combos.append(
            ParamCombo(values=vals, label=f"selective: {name}=rare value", weight=1.0)
        )

    ordered = baseline + single_null + date_sweep + multi_null + selective_combos

    # De-dup on the concrete value set (different axes can collide, e.g. an
    # optional param already NULL in the baseline) and cap the total.
    out: list[ParamCombo] = []
    seen: set[tuple] = set()
    for combo in ordered:
        key = tuple(sorted((k, str(v)) for k, v in combo.values.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(combo)
        if len(out) >= max_combos:
            break
    return out or None


# ---- 3. synthesize values per type -----------------------------------------

def _synth_values(param: ProcParam) -> list[object]:
    """Boundary + typical candidate values for a parameter, by type family."""
    t = param.sql_type.lower()
    if any(t.startswith(x) for x in ("int", "bigint", "smallint", "tinyint")):
        return [0, 1, 1000, 999999]            # low / typical / high cardinality
    if t.startswith(("decimal", "numeric", "money", "float", "real")):
        return [0, 1.5, 1000.0]
    if t.startswith(("date", "datetime", "smalldatetime", "datetime2")):
        return ["2020-01-01", "2024-06-01"]    # old vs recent (sniffing-sensitive)
    if t.startswith(("varchar", "nvarchar", "char", "nchar")):
        return ["A", "common_value", "rare_value"]
    if t.startswith("bit"):
        return [0, 1]
    if t.startswith("uniqueidentifier"):
        return ["00000000-0000-0000-0000-000000000000"]
    return [None]


def synthesize_combos(
    params: list[ProcParam],
    max_combos: int = 12,
) -> list[ParamCombo]:
    """Cartesian product of candidate values, capped at max_combos.

    Output params are excluded from the input value space.
    """
    inputs = [p for p in params if not p.is_output]
    if not inputs:
        return [ParamCombo(values={}, label="no-params")]

    value_lists = [_synth_values(p) for p in inputs]
    combos: list[ParamCombo] = []
    for i, combo_vals in enumerate(product(*value_lists)):
        if i >= max_combos:
            break
        values = {p.name: v for p, v in zip(inputs, combo_vals)}
        label = ", ".join(f"{k}={v}" for k, v in values.items())
        combos.append(ParamCombo(values=values, label=label[:80]))
    return combos


# ---- public entry point -----------------------------------------------------

def discover(
    cursor,
    proc_name: str,
    max_combos: int = 12,
    use_query_store: bool = True,
) -> tuple[list[ProcParam], list[ParamCombo]]:
    """Return (signature, candidate parameter combos) for ANY procedure.

    Workload sources are tried in priority order; the first that yields combos
    wins, and every source degrades safely to the next:
      1. SP_OPT_COMBOS file  — explicit, fully hand-curated workload.
      2. data-derived        — windows anchored to the proc's real column ranges.
      3. synthesized         — type-based boundary values (always succeeds).
    """
    params = get_signature(cursor, proc_name)

    combos = _combos_from_env()
    if combos is None:
        proc_text = get_proc_text(cursor, proc_name)
        try:
            combos = derive_combos_from_data(cursor, params, proc_text, max_combos=max_combos)
        except Exception:
            combos = None
    if combos is None:
        combos = synthesize_combos(params, max_combos=max_combos)
    return params, combos
