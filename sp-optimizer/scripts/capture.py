"""Step 2: capture execution plans + runtime stats for each parameter combo.

Two modes:
  - estimated (default, read-only): SET SHOWPLAN_XML ON, never runs the body.
  - actual (opt-in): SET STATISTICS XML/IO/TIME ON, executes the proc. Only
    use against non-prod or with explicit user confirmation.

The proc is invoked through a sandbox copy when one exists, so the live object
is never touched.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Optional

from .models import ParamCombo, PlanCapture

# ShowPlan namespace (actual plans carry RunTimeInformation / QueryTimeStats here)
_NS = {"sp": "http://schemas.microsoft.com/sqlserver/2004/07/showplan"}


def _attach_runtime(cap: PlanCapture) -> None:
    """Parse runtime stats (elapsed/CPU ms, logical reads, output rows) from an
    ACTUAL ShowPlan XML and attach them to the capture. Best-effort: leaves
    fields as None if the plan has no runtime section (e.g. estimated-only)."""
    if not cap.plan_xml:
        return
    try:
        root = ET.fromstring(cap.plan_xml)
    except ET.ParseError:
        return

    qts = root.find(".//sp:QueryTimeStats", _NS)
    if qts is not None:
        cap.cpu_ms = float(qts.get("CpuTime", "0") or 0)
        cap.elapsed_ms = float(qts.get("ElapsedTime", "0") or 0)

    # Logical reads: sum ActualLogicalReads across every per-thread counter.
    # Output rows: actual rows emitted by the statement's root RelOp.
    reads = 0
    saw_reads = False
    for rt in root.findall(".//sp:RunTimeCountersPerThread", _NS):
        v = rt.get("ActualLogicalReads")
        if v is not None:
            reads += int(v)
            saw_reads = True
    if saw_reads:
        cap.logical_reads = reads

    # Statement output rows = ActualRows of the topmost operator that actually
    # executed. RelOps are emitted in pre-order (parent before child), and the
    # true root (e.g. Compute Scalar) may carry no RunTimeInformation, so take
    # the FIRST RelOp that does — it's the highest executing op and is 1:1 with
    # the final result. Sum ActualRows across its per-thread counters.
    for rel in root.findall(".//sp:RelOp", _NS):
        rts = rel.findall("./sp:RunTimeInformation/sp:RunTimeCountersPerThread", _NS)
        rows = [rt.get("ActualRows") for rt in rts if rt.get("ActualRows") is not None]
        if rows:
            cap.output_rows = sum(float(r) for r in rows)
            break


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
    """Actual plan + runtime stats. EXECUTES the proc — non-prod / confirmed only.

    With SET STATISTICS XML ON, each executed statement returns its data result
    set followed by a single-cell result set holding the actual ShowPlan XML.
    We must FULLY drain each data set (so the statement runs to completion and
    the actual row counts are accurate) and then pick out the XML set, which is
    the last result set after the data."""
    args = _arg_list(combo)
    exec_stmt = f"EXEC {proc_name} {args};" if args else f"EXEC {proc_name};"
    try:
        cursor.execute("SET STATISTICS XML ON;")
        cursor.execute(exec_stmt)
        plan_xml = ""
        while True:
            try:
                rows = cursor.fetchall()
            except Exception:
                rows = None
            if rows:
                first = rows[0]
                # The actual-plan result set is a single row with a single XML cell.
                if len(first) == 1 and isinstance(first[0], str) and first[0].lstrip().startswith("<"):
                    plan_xml = first[0]
            if not cursor.nextset():
                break
        cursor.execute("SET STATISTICS XML OFF;")
        cap = PlanCapture(combo=combo, plan_xml=plan_xml)
        _attach_runtime(cap)
        return cap
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
