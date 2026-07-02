"""Step 2: capture execution plans + runtime stats for each parameter combo.

Two modes:
  - estimated (default, read-only): SET SHOWPLAN_XML ON, never runs the body.
  - actual (opt-in): SET STATISTICS XML/IO/TIME ON, executes the proc. Only
    use against non-prod or with explicit user confirmation.

The proc is invoked through a sandbox copy when one exists, so the live object
is never touched.
"""
from __future__ import annotations

import re
import statistics
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


def _drain_messages(cursor, sink: list[str]) -> None:
    """Collect any pending informational messages (SET STATISTICS IO / TIME
    output arrives here, not as a result set) into ``sink``. Best-effort: the
    driver may not expose ``cursor.messages`` at all, in which case we no-op.

    pyodbc populates ``cursor.messages`` as a list of ``(state, text)`` tuples
    for the most recent operation and clears it on the next one, so this must be
    called once per result set (before ``nextset``) to catch everything."""
    msgs = getattr(cursor, "messages", None)
    if not msgs:
        return
    for m in msgs:
        text = m[1] if isinstance(m, (list, tuple)) and len(m) > 1 else str(m)
        # The ODBC layer prefixes driver/source tags like
        # "[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"; strip the
        # leading bracket run so the IO-stat text reads cleanly.
        cleaned = re.sub(r"^(\[[^\]]*\])+", "", str(text)).strip()
        if cleaned and (not sink or sink[-1] != cleaned):
            sink.append(cleaned)


# Ignorable idle waits that would otherwise dominate every session snapshot.
_IDLE_WAITS = {
    "WAITFOR", "SLEEP_TASK", "BROKER_RECEIVE_WAITFOR",
    "LAZYWRITER_SLEEP", "SQLTRACE_INCREMENTAL_FLUSH_SLEEP",
}

_SESSION_WAITS_SQL = """
SELECT wait_type, wait_time_ms
FROM sys.dm_exec_session_wait_stats
WHERE session_id = @@SPID AND wait_time_ms > 0;
"""


def _session_wait_snapshot(cursor) -> Optional[dict[str, float]]:
    """Cumulative session waits (wait_type -> ms). Best-effort: returns None
    when the DMV isn't visible (permissions/edition), so callers degrade."""
    try:
        cursor.execute(_SESSION_WAITS_SQL)
        return {r.wait_type: float(r.wait_time_ms) for r in cursor.fetchall()}
    except Exception:
        return None


def _wait_delta(before: Optional[dict], after: Optional[dict], top: int = 5) -> Optional[dict]:
    """Waits accumulated between two snapshots, top-N by time, idle waits dropped."""
    if before is None or after is None:
        return None
    delta = {}
    for wt, ms in after.items():
        d = ms - before.get(wt, 0.0)
        if d > 0 and wt not in _IDLE_WAITS:
            delta[wt] = round(d, 1)
    if not delta:
        return None
    return dict(sorted(delta.items(), key=lambda kv: kv[1], reverse=True)[:top])


def capture_actual(cursor, proc_name: str, combo: ParamCombo) -> PlanCapture:
    """Actual plan + runtime stats. EXECUTES the proc — non-prod / confirmed only.

    With SET STATISTICS XML ON, each executed statement returns its data result
    set followed by a single-cell result set holding the actual ShowPlan XML.
    We must FULLY drain each data set (so the statement runs to completion and
    the actual row counts are accurate) and then pick out the XML set, which is
    the last result set after the data. SET STATISTICS IO / TIME are also turned
    on so the per-table logical/physical read counts and CPU/elapsed timings are
    captured as text evidence (collected from the message stream)."""
    args = _arg_list(combo)
    exec_stmt = f"EXEC {proc_name} {args};" if args else f"EXEC {proc_name};"
    io_msgs: list[str] = []
    waits_before = _session_wait_snapshot(cursor)
    try:
        cursor.execute("SET STATISTICS XML ON; SET STATISTICS IO ON; SET STATISTICS TIME ON;")
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
            _drain_messages(cursor, io_msgs)
            if not cursor.nextset():
                break
        _drain_messages(cursor, io_msgs)
        cursor.execute("SET STATISTICS XML OFF; SET STATISTICS IO OFF; SET STATISTICS TIME OFF;")
        cap = PlanCapture(
            combo=combo,
            plan_xml=plan_xml,
            io_stats_text="\n".join(io_msgs).strip() or None,
            wait_stats=_wait_delta(waits_before, _session_wait_snapshot(cursor)),
        )
        _attach_runtime(cap)
        return cap
    except Exception as e:
        try:
            cursor.execute("SET STATISTICS XML OFF; SET STATISTICS IO OFF; SET STATISTICS TIME OFF;")
        except Exception:
            pass
        return PlanCapture(
            combo=combo, plan_xml="",
            io_stats_text="\n".join(io_msgs).strip() or None,
            error=str(e),
        )


def capture_actual_repeated(cursor, proc_name: str, combo: ParamCombo, runs: int) -> PlanCapture:
    """Run the combo (runs + 1) times — one discarded warm-up, then ``runs``
    measured executions — and report the MEDIAN elapsed/CPU/reads.

    A single execution mixes compile time and cold-cache IO into the numbers,
    so a 10% 'win' can be pure noise; the warm-up absorbs compilation and the
    median resists outliers. The representative capture (plan XML, IO text,
    waits) is the run with the median elapsed time, its headline metrics
    replaced by the per-metric medians."""
    if runs <= 1:
        return capture_actual(cursor, proc_name, combo)

    capture_actual(cursor, proc_name, combo)  # warm-up: compile + buffer pool
    caps = [capture_actual(cursor, proc_name, combo) for _ in range(runs)]
    ok = [c for c in caps if not c.error and c.plan_xml]
    if not ok:
        return caps[-1]

    def _median(vals):
        vals = [v for v in vals if v is not None]
        return statistics.median(vals) if vals else None

    med_elapsed = _median([c.elapsed_ms for c in ok])
    # pick the run closest to the median elapsed as the representative capture
    rep = min(ok, key=lambda c: abs((c.elapsed_ms or 0) - (med_elapsed or 0)))
    rep.elapsed_ms = med_elapsed
    rep.cpu_ms = _median([c.cpu_ms for c in ok])
    reads = _median([c.logical_reads for c in ok])
    rep.logical_reads = int(reads) if reads is not None else None
    return rep


def capture_workload(
    cursor,
    proc_name: str,
    combos: list[ParamCombo],
    actual: bool = False,
    runs: int = 1,
) -> list[PlanCapture]:
    if actual:
        return [capture_actual_repeated(cursor, proc_name, c, runs) for c in combos]
    return [capture_estimated(cursor, proc_name, c) for c in combos]
