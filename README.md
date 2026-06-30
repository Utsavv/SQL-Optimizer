# sp-optimizer

AI-driven, iterative optimizer for SQL Server stored procedures.

It treats tuning as a closed feedback loop: discover the parameter space →
capture execution plans across that space → analyze plans deterministically →
let an LLM propose one smallest-safe change → apply to a **sandbox copy** →
re-verify → repeat until the *majority* of parameter calls land on a good plan.

This directly targets parameter sniffing, where a proc compiled for one set of
arguments performs badly for others.

## Why this is different from existing tools

Existing tools (PerformanceStudio, PerformanceMonitor, SQL MCP Server) do
one-shot plan analysis or monitoring. None of them run an autonomous
**discover → change → re-verify** loop scored across the whole parameter
workload. That loop is the contribution here.

## Install

```bash
pip install pyodbc
# choose one backend:
pip install google-cloud-aiplatform vertexai   # Gemini (Vertex AI)
pip install anthropic                           # Claude
```

Requires ODBC Driver 18 for SQL Server.

## Run

```bash
python -m scripts.optimize \
  --proc "dbo.usp_GetMemberActivity" \
  --conn "Driver={ODBC Driver 18 for SQL Server};Server=.;Database=Loyalty;Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes" \
  --backend gemini --project my-gcp-project \
  --max-iterations 5 --target-fraction 0.8 \
  --report out/report.md
```

Add `--actual` to capture runtime stats (executes the proc — **non-prod only**).

Works against **any** stored procedure — just change `--proc`. The workload is
derived from the target proc's own column ranges, so no per-proc setup is
needed. `examples/worldwideimporters/` is one fully worked run for reference.

## Architecture

| Module | Role | LLM? |
|---|---|---|
| `discover.py` | parameter space → workload combos, **auto-derived from the proc's real data** | no |
| `capture.py` | execution plan + runtime capture | no |
| `analyze.py` | deterministic plan scoring | no |
| `llm.py` | propose one safe change as JSON | **yes** |
| `optimize.py` | the loop + sandbox + CLI + report | no |

The model is only used at the decision step. Everything else is deterministic
and testable offline.

## Safety

- The live procedure is **never** modified — changes go to `<proc>_opt_v<n>`.
- Estimated plans are read-only; actual execution is opt-in.
- Every change carries a rollback; all changes are written to `changes.sql`.

## Status

Scaffold / v0. The deterministic pipeline (discover + analyze) is tested
offline, including the data-derived workload generator (`derive_combos_from_data`)
that makes the skill generic across procedures. The DB-facing steps need a live
SQL Server to exercise. Mining concrete argument values from Query Store and
mapping more predicate shapes (multi-column, function-wrapped) are the next
enhancements.
