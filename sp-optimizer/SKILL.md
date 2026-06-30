---
name: sp-optimizer
description: Iteratively optimize SQL Server stored procedures using an AI-driven feedback loop. Use this skill whenever the user wants to tune, optimize, or improve the performance of a SQL Server stored procedure — especially when they mention parameter sniffing, execution plans, slow procedures, query tuning, or want an agent to automatically analyze and rewrite a procedure. The skill discovers representative parameter combinations, captures and analyzes execution plans for each, proposes and applies changes, then re-runs the loop until most parameter calls land on a good plan.
---

# SP Optimizer

An AI-driven, iterative optimizer for SQL Server stored procedures. It treats optimization as a closed feedback loop: discover the parameter space, capture execution plans across that space, analyze the plans, apply targeted changes, then repeat until the procedure performs well across the *majority* of realistic parameter calls — not just one lucky compile.

## When to use

Trigger this whenever the user wants to make a stored procedure faster, mentions parameter sniffing, asks to analyze execution plans, or wants an autonomous loop that tunes a procedure. Works against on-prem SQL Server (2016+), Azure SQL MI, and AWS RDS for SQL Server.

This skill is **procedure-agnostic** — point it at *any* stored procedure in any database with `--proc`. It reads the proc's signature, derives a representative workload from that proc's own data, and tunes it. Nothing is hard-wired to a particular schema or proc; the WorldWideImporters files under `examples/` are just one worked run, not a dependency.

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
  │               index, rewrite), grounded in cached or fresh    │
  │               Microsoft Learn guidance. Stop if good enough.  │
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
4. **Never auto-create indexes on production** without surfacing the cost/space impact and getting confirmation. Any proposed index must follow `references/indexing-best-practices.md` — an extra index is a permanent write tax, so the bar is deliberately high.

## Operating procedure for any stored procedure

The same flow applies to every proc — substitute the name and connection; nothing else is proc-specific.

1. **Name the target.** Any schema-qualified proc: `--proc "<schema>.<proc>"`. No allow-list, no per-proc setup.
2. **Connect.** Pass `--conn` or set `SQL_CONNECTION_STRING` in `.env`; the CLI reads it automatically.
3. **Let the workload be derived.** `discover.py` reads the signature from `sys.parameters`, then builds the workload **from the proc's own data**: it maps each parameter to the column it filters, reads that column's real min/max, and fans out narrow → medium → wide → empty windows (the spread that exposes parameter-sniffing skew). No hand-written combos are needed for the common date-range case. You can still override with a curated `SP_OPT_COMBOS` file when you want exact values.
4. **Run the loop.** Capture → analyze → decide → apply-to-sandbox → re-verify, until a termination condition is met.
5. **Review outputs** under the run's output dir: the report, the applied changes + rollbacks, and the winning variant.

### Typical entry point

```bash
python -m scripts.optimize \
  --proc "<schema>.<your_proc>" \
  --backend litellm --model "gemini/gemini-1.5-flash" \
  --max-iterations 5 \
  --target-fraction 0.8 \
  --report out/report.md
# --conn is read from SQL_CONNECTION_STRING (.env) if omitted.
# --model defaults to LLM_MODEL (.env) if omitted.
# add --actual to capture runtime stats (executes the proc — non-prod only).
```

Walk through the modules in this order when reading or extending the code:
1. `scripts/discover.py` — parameter discovery + data-derived workload (see `references/parameter-discovery.md`)
2. `scripts/capture.py` — execution plan + runtime capture
3. `scripts/analyze.py` — plan XML scoring (see `references/plan-analysis.md`)
4. `scripts/optimize.py` — the orchestration loop + LLM decision step
5. `scripts/llm.py` — pluggable LLM backend (any provider via LiteLLM, or replay); the decision prompt encodes the index discipline from `references/indexing-best-practices.md`

## Decision grounding (Microsoft Learn MCP, cached)

When the decision step (step 4) is driven by an interactive agent (e.g. via
`--backend file` with Microsoft Learn MCP available — see `FileBackend` in
`scripts/llm.py`), ground every proposed change in official guidance, but
**check the cache before paying for a fresh lookup**:

1. **Read `references/decision-log.md` first.** Match the current finding
   (warning type, signal, or proposed change `kind`) against each entry's
   `Keywords:` line.
2. **Cache hit** — an entry already answers the question: use its
   `Takeaway:` to make the decision and cite its `Source:` URL(s) in the
   rationale. Do not call Microsoft Learn MCP.
3. **Cache miss** — no entry covers it, or the scenario meaningfully differs
   (different warning type, version, or conflicting signal): call
   `microsoft_docs_search` (and `microsoft_docs_fetch` if needed), make the
   decision, then **append a new entry** to `references/decision-log.md` in
   the format documented at the top of that file so future runs hit the
   cache instead of re-querying.

This keeps token spend on MCP search/fetch calls limited to genuinely new
questions instead of re-researching the same parameter-sniffing or
indexing patterns on every run.

## LLM backend

The decision step (step 4) is the only place an LLM is required. It is pluggable: `scripts/llm.py` exposes a `propose_change(context) -> Change` interface with two implementations — `LiteLLMBackend` (default; routes to any provider — OpenAI, Anthropic, Gemini, Azure, Bedrock, etc. — via the `LLM_MODEL` env var / `--model` flag and the matching API key, no code change needed to switch) and `FileBackend` (`--backend file --decisions <path>`), which replays agent-made decisions from JSON when no model API key is available. The analysis and capture steps are deterministic and need no model.

The structured JSON prompt that drives the decision step lives in `scripts/llm.py` (`SYSTEM_PROMPT`) — it asks for a single, smallest-safe change plus rationale and a rollback, returned as strict JSON. None of it references any specific procedure, so it applies unchanged to whatever proc you target.

## Output

A run produces:
- An HTML/Markdown report: per-iteration workload scores, plan thumbnails, the change applied, and before/after comparison.
- A `changes.sql` file with every applied change and its rollback.
- A `winner.sql` containing the best-performing procedure variant.

## Examples

`examples/worldwideimporters/` holds one fully worked run (a covering index + `OPTION (RECOMPILE)` on `Integration.GetMovementUpdates`). It is illustrative only — useful to see the shape of `combos.json`, `decisions.json`, and `winner.sql` — and is **not** required to run the skill against your own proc.

## Extending

- New plan-analysis rules go in `scripts/analyze.py` and are documented in `references/plan-analysis.md`.
- To add a parameter-value strategy (e.g. pull real distributions from Query Store or a stats histogram, or map more predicate shapes), extend `scripts/discover.py` — `derive_combos_from_data()` is the data-anchored generator that keeps the workload generic across procs.
- Indexing guidance the LLM must follow when proposing an index lives in `references/indexing-best-practices.md`; keep the `SYSTEM_PROMPT` in `scripts/llm.py` in sync with it.
- Every Microsoft Learn MCP lookup made during a decision step should leave a new entry in `references/decision-log.md` (format documented at the top of that file) so the next run can reuse it instead of re-querying.
