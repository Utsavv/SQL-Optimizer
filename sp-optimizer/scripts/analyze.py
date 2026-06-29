"""Step 3: deterministic execution-plan analysis and scoring.

Parses ShowPlan XML and produces a 0..100 quality score plus structured
signals the LLM can reason over. No model is used here — this is the
trustworthy, repeatable backbone of the loop.

Scoring is penalty-based: start at 100, subtract for each anti-pattern found,
weighted by estimated subtree cost where available.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from .models import PlanCapture, PlanScore

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

    # --- scans on large inputs (penalty scales with estimated rows) ---
    scan_count = 0
    for rel in _findall(root, "RelOp"):
        phys = rel.get("PhysicalOp", "")
        est_rows = float(rel.get("EstimateRows", "0") or 0)
        if "Scan" in phys and "Index Scan" not in phys:
            scan_count += 1
            penalty = min(20.0, 5.0 + est_rows / 100000.0)
            score -= penalty
            warnings.append(f"{phys} (~{est_rows:.0f} rows)")
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
    conversions = 0
    for conv in _findall(root, "Convert"):
        if conv.get("Implicit") == "1":
            conversions += 1
    if conversions:
        score -= min(10.0, conversions * 2.0)
        warnings.append(f"{conversions} implicit conversion(s)")
    signals["implicit_conversion_count"] = conversions

    # --- estimated vs actual row skew (only present in actual plans) ---
    skew_ops = 0
    for rel in _findall(root, "RelOp"):
        est = float(rel.get("EstimateRows", "0") or 0)
        rt = rel.find(".//sp:RunTimeInformation/sp:RunTimeCountersPerThread", NS)
        if rt is not None:
            actual = float(rt.get("ActualRows", "0") or 0)
            if est > 0 and (actual / est > 10 or est / max(actual, 1) > 10):
                skew_ops += 1
    if skew_ops:
        score -= min(20.0, skew_ops * 5.0)
        warnings.append(f"{skew_ops} op(s) with >10x estimate skew (sniffing?)")
    signals["estimate_skew_ops"] = skew_ops

    score = max(0.0, min(100.0, score))
    return PlanScore(
        combo_label=label,
        score=score,
        warnings=warnings,
        missing_indexes=missing,
        signals=signals,
    )


def analyze_workload(captures: list[PlanCapture]) -> list[PlanScore]:
    return [analyze_plan(c) for c in captures]
