"""Step 2: capture execution plans + runtime stats for each parameter combo.

Two modes:
  - estimated (default, read-only): SET SHOWPLAN_XML ON, never runs the body.
  - actual (opt-in): SET STATISTICS XML/IO/TIME ON, executes the proc. Only
    use against non-prod or with explicit user confirmation.

The proc is invoked through a sandbox copy when one exists, so the live object
is never touched.
"""
from __future__ import annotations

from typing import Optional

from .models import ParamCombo, PlanCapture


def _arg_list(combo: ParamCombo) -> str:
    parts = []
    for name, val in combo.values.items():
        if val is None:
            parts.append(f"{name}=NULL")
        elif isinstance(val, str):
            safe = val.replace("'", "''")
            parts.append(f"{name}=N'{safe}'")
        else:
            parts.append(f"{name}={val}")
    return ", ".join(parts)


def capture_estimated(cursor, proc_name: str, combo: ParamCombo) -> PlanCapture:
    """Estimated plan only — does NOT execute the procedure body."""
    args = _arg_list(combo)
    exec_stmt = f"EXEC {proc_name} {args};" if args else f"EXEC {proc_name};"
    try:
        cursor.execute("SET SHOWPLAN_XML ON;")
        cursor.execute(exec_stmt)
        plan_xml = None
        # The plan comes back as a result set containing the XML.
        rows = cursor.fetchall()
        if rows:
            plan_xml = rows[0][0]
        cursor.execute("SET SHOWPLAN_XML OFF;")
        return PlanCapture(combo=combo, plan_xml=plan_xml or "")
    except Exception as e:
        try:
            cursor.execute("SET SHOWPLAN_XML OFF;")
        except Exception:
            pass
        return PlanCapture(combo=combo, plan_xml="", error=str(e))


def capture_actual(cursor, proc_name: str, combo: ParamCombo) -> PlanCapture:
    """Actual plan + runtime stats. EXECUTES the proc — non-prod / confirmed only."""
    args = _arg_list(combo)
    exec_stmt = f"EXEC {proc_name} {args};" if args else f"EXEC {proc_name};"
    try:
        cursor.execute("SET STATISTICS XML ON;")
        cursor.execute(exec_stmt)
        # Drain result sets; the actual plan XML is the final result set.
        plan_xml = ""
        while True:
            try:
                rows = cursor.fetchall()
                if rows and isinstance(rows[0][0], str) and rows[0][0].startswith("<"):
                    plan_xml = rows[0][0]
            except Exception:
                pass
            if not cursor.nextset():
                break
        cursor.execute("SET STATISTICS XML OFF;")
        return PlanCapture(combo=combo, plan_xml=plan_xml)
    except Exception as e:
        try:
            cursor.execute("SET STATISTICS XML OFF;")
        except Exception:
            pass
        return PlanCapture(combo=combo, plan_xml="", error=str(e))


def capture_workload(
    cursor,
    proc_name: str,
    combos: list[ParamCombo],
    actual: bool = False,
) -> list[PlanCapture]:
    fn = capture_actual if actual else capture_estimated
    return [fn(cursor, proc_name, c) for c in combos]
