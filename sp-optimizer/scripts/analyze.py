"""Step 3: deterministic execution-plan analysis and scoring.

Parses ShowPlan XML and produces a 0..100 quality score plus structured
signals the LLM can reason over. No model is used here — this is the
trustworthy, repeatable backbone of the loop.

Scoring is penalty-based: start at 100, subtract for each anti-pattern found,
weighted by estimated subtree cost where available.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET

from .models import PlanCapture, PlanScore

# Physical operators that read an entire input (vs. a seek into a subset).
_FULL_SCAN_OPS = {"Table Scan", "Clustered Index Scan", "Index Scan"}

# ShowPlan namespace
NS = {"sp": "http://schemas.microsoft.com/sqlserver/2004/07/showplan"}


def _findall(root, tag: str):
    return root.findall(f".//sp:{tag}", NS)


def analyze_plan(cap: PlanCapture) -> PlanScore:
    label = cap.combo.label or "default"
    if cap.error or not cap.plan_xml:
        return PlanScore(
            combo_label=label,
            score=0.0,
            warnings=[f"capture failed: {cap.error or 'empty plan'}"],
        )

    try:
        root = ET.fromstring(cap.plan_xml)
    except ET.ParseError as e:
        return PlanScore(combo_label=label, score=0.0, warnings=[f"xml parse error: {e}"])

    score = 100.0
    warnings: list[str] = []
    missing: list[str] = []
    signals: dict = {}

    # --- full scans on large inputs (penalty scales with estimated rows) ---
    # A full Table/Clustered Index/Index Scan over a large input is the classic
    # symptom of a missing access path. We gate on estimated rows so a tiny
    # scan (already cheap) isn't punished like a full-table sweep.
    scan_count = 0
    scan_penalty_total = 0.0
    for rel in _findall(root, "RelOp"):
        phys = rel.get("PhysicalOp", "")
        est_rows = float(rel.get("EstimateRows", "0") or 0)
        if phys in _FULL_SCAN_OPS and est_rows >= 10000:
            scan_count += 1
            penalty = min(20.0, 5.0 + est_rows / 50000.0)
            scan_penalty_total += penalty
            warnings.append(f"{phys} (~{est_rows:.0f} est rows)")
    # Cap total scan penalty so multi-statement procs (N sequential INSERTs)
    # don't accumulate N×penalties and zero out the score unconditionally.
    score -= min(40.0, scan_penalty_total)
    signals["table_scan_count"] = scan_count

    # --- missing index suggestions ---
    for mi in _findall(root, "MissingIndexGroup"):
        impact = float(mi.get("Impact", "0") or 0)
        score -= min(15.0, impact / 5.0)
        for idx in mi.findall(".//sp:MissingIndex", NS):
            tbl = idx.get("Table", "?")
            missing.append(f"{tbl} (impact {impact:.0f}%)")
    signals["missing_index_count"] = len(missing)

    # --- key lookups ---
    lookups = sum(1 for r in _findall(root, "RelOp")
                  if r.get("PhysicalOp") == "Key Lookup")
    if lookups:
        score -= min(15.0, lookups * 3.0)
        warnings.append(f"{lookups} key lookup(s)")
    signals["key_lookup_count"] = lookups

    # --- spills / sort warnings ---
    spills = len(_findall(root, "SpillToTempDb"))
    if spills:
        score -= min(20.0, spills * 8.0)
        warnings.append(f"{spills} tempdb spill(s)")
    signals["spill_count"] = spills

    # --- implicit conversions in predicates (sniffing/SARGability red flag) ---
    # Only count conversions inside Predicate or SeekPredicates elements.
    # Conversions inside ComputeScalar/concatenation are harmless type promotions
    # and must not be reported as SARGability issues.
    conversions = 0
    predicate_roots = (
        _findall(root, "Predicate")
        + _findall(root, "SeekPredicates")
    )
    for pred in predicate_roots:
        for conv in pred.findall(".//sp:Convert", NS):
            if conv.get("Implicit") == "1":
                conversions += 1
    if conversions:
        score -= min(10.0, conversions * 2.0)
        warnings.append(f"{conversions} implicit conversion(s) in predicate(s)")
    signals["implicit_conversion_count"] = conversions

    # --- estimated vs actual row skew (only present in actual plans) ---
    skew_ops = 0
    for rel in _findall(root, "RelOp"):
        est = float(rel.get("EstimateRows", "0") or 0)
        # Each RelOp carries its OWN runtime counters as a direct child; using a
        # descendant axis (.//) here would let an outer operator pick up a nested
        # child's counters and compare them against the parent's estimate, so
        # match only the immediate child (same pattern as capture._attach_runtime).
        rts = rel.findall("./sp:RunTimeInformation/sp:RunTimeCountersPerThread", NS)
        if rts:
            actual = sum(float(rt.get("ActualRows", "0") or 0) for rt in rts)
            if est > 0 and (actual / est > 10 or est / max(actual, 1) > 10):
                skew_ops += 1
    if skew_ops:
        score -= min(20.0, skew_ops * 5.0)
        warnings.append(f"{skew_ops} op(s) with >10x estimate skew (sniffing?)")
    signals["estimate_skew_ops"] = skew_ops

    # --- runtime inefficiency: many logical reads to emit few rows ---
    # Actual-mode only. Reading the whole table (high logical reads) to return a
    # small result is the runtime fingerprint of a missing covering index. This
    # is removed once the seek-friendly index exists.
    if cap.logical_reads is not None and cap.logical_reads > 500:
        out_rows = max(cap.output_rows or 0.0, 1.0)
        reads_per_row = cap.logical_reads / out_rows
        signals["reads_per_row"] = round(reads_per_row, 1)
        if reads_per_row > 5.0:
            penalty = min(25.0, 5.0 + 6.0 * math.log10(reads_per_row))
            score -= penalty
            warnings.append(
                f"{cap.logical_reads} logical reads for {int(out_rows)} row(s) "
                f"(reads/row={reads_per_row:.1f})"
            )

    score = max(0.0, min(100.0, score))
    return PlanScore(
        combo_label=label,
        score=score,
        warnings=warnings,
        missing_indexes=missing,
        signals=signals,
        elapsed_ms=cap.elapsed_ms,
        cpu_ms=cap.cpu_ms,
        logical_reads=cap.logical_reads,
        output_rows=cap.output_rows,
    )


def analyze_workload(captures: list[PlanCapture]) -> list[PlanScore]:
    return [analyze_plan(c) for c in captures]
