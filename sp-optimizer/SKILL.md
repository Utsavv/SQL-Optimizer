---
name: sp-optimizer
description: Iteratively optimize SQL Server stored procedures using an AI-driven feedback loop. Use this skill whenever the user wants to tune, optimize, or improve the performance of a SQL Server stored procedure — especially when they mention parameter sniffing, execution plans, slow procedures, query tuning, or want an agent to automatically analyze and rewrite a procedure. The skill discovers representative parameter combinations, captures and analyzes execution plans for each, proposes and applies changes, then re-runs the loop until most parameter calls land on a good plan.
---

# SP Optimizer

An AI-driven, iterative optimizer for SQL Server stored procedures. It treats optimization as a closed feedback loop: discover the parameter space, capture execution plans across that space, analyze the plans, apply targeted changes, then repeat until the procedure performs well across the *majority* of realistic parameter calls — not just one lucky compile.

## When to use

Trigger this whenever the user wants to make a stored procedure faster, mentions parameter sniffing, asks to analyze execution plans, or wants an autonomous loop that tunes a procedure. Works against on-prem SQL Server (2016+), Azure SQL MI, and AWS RDS for SQL Server.

## The core loop

```
  ┌─────────────────────────────────────────────────────────────┐
  │ 1. DISCOVER   parse SP signature → generate representative    │
  │               parameter value combinations (the "workload")  │
  │ 2. CAPTURE    for each combo, get the estimated + actual      │
  │               execution plan and runtime stats                │
  │ 3. ANALYZE    parse plan XML → score plans, find bottlenecks  │
  │               (scans, spills, sniffing skew, missing indexes) │
  │ 4. DECIDE     LLM proposes targeted changes (hints, OPTION,   │
  │               index, rewrite). Stop if already good enough.   │
  │ 5. APPLY      apply change to a sandbox copy of the SP        │
  │ 6. VERIFY     re-capture plans across the SAME workload       │
  │               → did the majority improve without regressions? │
  └──────────────────────────┬──────────────────────────────────┘
                             │ repeat 2–6 until termination
                             ▼
                  best variant + full report
```

## Termination conditions

Stop the loop when ANY of these is true:
- A target fraction (default 80%) of parameter combinations land on a plan scoring at/above the quality threshold, AND no combination regressed beyond a tolerance.
- `max_iterations` reached (default 5).
- Two consecutive iterations produce no net improvement in the aggregate workload score.
- The LLM explicitly declares it has no safe change left to propose.

## Safety rules (non-negotiable)

1. **Never modify the live procedure.** All changes are applied to a sandbox copy (`<proc>_opt_v<n>`) or wrapped in an explicit transaction the user must approve before commit.
2. **Read-only by default for plan capture.** Use estimated plans + Query Store where possible; only run actual execution against non-production or with explicit user confirmation.
3. **Always produce a diff and a rollback script** for any change before applying it.
4. **Never auto-create indexes on production** without surfacing the cost/space impact and getting confirmation.

## How to run

The skill is a Python package under `scripts/`. The typical entry point:

```bash
python -m scripts.optimize \
  --proc "dbo.usp_GetMemberActivity" \
  --conn "Driver={ODBC Driver 18 for SQL Server};Server=...;Database=Loyalty;Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes" \
  --max-iterations 5 \
  --target-fraction 0.8 \
  --sandbox \
  --report out/report.html
```

Walk through the modules in this order when reading or extending the code:
1. `scripts/discover.py` — parameter discovery (see `references/parameter-discovery.md`)
2. `scripts/capture.py` — execution plan + runtime capture
3. `scripts/analyze.py` — plan XML scoring (see `references/plan-analysis.md`)
4. `scripts/optimize.py` — the orchestration loop + LLM decision step
5. `scripts/llm.py` — pluggable LLM backend (Gemini via Vertex AI, or Claude)

## LLM backend

The decision step (step 4) is the only place an LLM is required. It is pluggable: `scripts/llm.py` exposes a `propose_change(context) -> Change` interface with two implementations — `GeminiBackend` (Vertex AI, matches the user's existing stack) and `ClaudeBackend`. The analysis and capture steps are deterministic and need no model.

See `references/prompt-templates.md` for the structured JSON prompt that drives the decision step — it asks for a single, smallest-safe change plus rationale and a rollback, returned as strict JSON.

## Output

A run produces:
- An HTML/Markdown report: per-iteration workload scores, plan thumbnails, the change applied, and before/after comparison.
- A `changes.sql` file with every applied change and its rollback.
- A `winner.sql` containing the best-performing procedure variant.

## Extending

- New plan-analysis rules go in `scripts/analyze.py` and are documented in `references/plan-analysis.md`.
- To add a parameter-value strategy (e.g. pull real distributions from Query Store or a stats histogram), extend `scripts/discover.py`.
