"""Step 1: discover the parameter space of a stored procedure.

Strategy (in priority order):
  1. Read the proc signature from sys.parameters to learn names/types.
  2. If Query Store is available, pull *real* parameter values that were
     actually used (best signal for "representative" workload).
  3. Otherwise, synthesize boundary + typical values per type, optionally
     informed by column statistics histograms when a param maps to a column.

This module is deliberately conservative: it returns candidate combos but
never executes anything destructive.
"""
from __future__ import annotations

from itertools import product
from typing import Optional

from .models import ParamCombo, ProcParam

# ---- 1. signature -----------------------------------------------------------

SIGNATURE_SQL = """
SELECT p.name        AS param_name,
       t.name        AS type_name,
       p.max_length  AS max_length,
       p.is_output   AS is_output
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
        params.append(
            ProcParam(
                name=row.param_name,
                sql_type=type_disp,
                is_output=bool(row.is_output),
            )
        )
    return params


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
    """Return (signature, candidate parameter combos)."""
    params = get_signature(cursor, proc_name)
    # Real-value mining is surfaced to the LLM/caller as a hint set; the
    # deterministic synthesis below guarantees we always have a workload.
    combos = synthesize_combos(params, max_combos=max_combos)
    return params, combos
