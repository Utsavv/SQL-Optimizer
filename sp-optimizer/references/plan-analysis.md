# Plan Analysis Rules

`scripts/analyze.py` scores each execution plan deterministically (no LLM).
Scoring is penalty-based: start at 100, subtract for anti-patterns.

| Signal | How detected (ShowPlan XML) | Penalty |
|---|---|---|
| Table/clustered scan on large input | `RelOp/@PhysicalOp` contains "Scan", scaled by `@EstimateRows` | 5–20 per scan, total capped at 40 |
| Missing index suggestion | `MissingIndexGroup/@Impact` | up to 15 |
| Key lookup | `RelOp/@PhysicalOp = "Key Lookup"` | 3 each, cap 15 |
| Tempdb spill | `SpillToTempDb` element present | 8 each, cap 20 |
| Implicit conversion | `Convert/@Implicit = "1" inside Predicate or SeekPredicates only` | 2 each, cap 10 |
| Estimate vs actual skew >10x | compare `@EstimateRows` to `RunTimeCountersPerThread/@ActualRows` | 5 each, cap 20 |

The skew rule is the key parameter-sniffing detector, but it only fires on
**actual** plans (estimated plans have no runtime counters). For read-only
estimated-plan runs, sniffing is inferred indirectly from scan + missing-index
signals plus comparing plan shapes across the workload.

## Adding a rule

1. Add detection logic in `analyze_plan()` that reads from the parsed XML root.
2. Subtract a bounded penalty and append a human-readable `warning`.
3. Record a raw value in `signals[...]` so the LLM decision step can use it.
4. Document the rule in the table above.

## Namespace gotcha

All ShowPlan elements live in
`http://schemas.microsoft.com/sqlserver/2004/07/showplan`. Always query with the
`sp:` prefix via the `NS` dict, or `findall` returns nothing.
