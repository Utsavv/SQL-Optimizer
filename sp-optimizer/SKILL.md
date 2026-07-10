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
  │ 1. DISCOVER   parse SP signature → explore the tables it      │
  │               filters for real values + enumerate optional-   │
  │               param NULL combinations (the "workload")        │
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
3. **Let the workload be derived.** `discover.py` reads the signature from `sys.parameters`, then builds the workload **from the proc's own data**: it maps each parameter to the column it filters and mines that column's real contents. Datetime range/bound params fan out narrow → medium → wide → empty windows (the spread that exposes parameter-sniffing skew); equality/other params are exercised with real frequency-ranked values (a hot common value and a selective rare one) that actually exist in the table. On top of that, **optional / catch-all params** (declared with a default, or guarded by `@p IS NULL OR …` / `ISNULL` / `COALESCE`) are enumerated on the NULL-vs-supplied axis, so every combination of "which optional filters are active" — each a potentially different plan — is captured. No hand-written combos are needed; you can still override with a curated `SP_OPT_COMBOS` file when you want exact values, and raise `--max-combos` when a proc has many optional params.
4. **Run the loop.** Capture → analyze → decide → apply-to-sandbox → re-verify, until a termination condition is met.
5. **Review outputs** under the run's output dir: the report, the applied changes + rollbacks, and the winning variant.

### Entry point — YOU (the agent) drive the steps

**This is the recommended path when a coding agent is running the skill.** You
are the decision-maker, so you do not need LiteLLM, an API key, or a pre-staged
`FileBackend` decisions file. Instead, drive the loop one step at a time with
`python -m scripts.session`: the deterministic engine runs discover / capture /
analyze / apply for you, and **you make the single "smallest-safe change"
decision yourself**, in your own reasoning, between `evaluate` and `apply`.

```bash
# Run from the sp-optimizer/ subdirectory — it holds the scripts/ package.
# (Running from the parent repo root fails: ModuleNotFoundError: No module named 'scripts'.)
cd sp-optimizer

# 1. DISCOVER — derive the workload and open a run. Prints the session path
#    (session.json inside the run folder) + the combos. --conn defaults to
#    SQL_CONNECTION_STRING (.env). Add --actual for runtime stats (non-prod only).
python -m scripts.session discover --proc "<schema>.<your_proc>" \
  --max-iterations 5 --target-fraction 0.8 --out-dir out

# Then loop, using the printed session path (out/<schema.proc>/<timestamp>/session.json):

# 2. EVALUATE — capture + analyze the current variant across the workload and
#    persist evidence. Prints the analysis as JSON (per-combo scores, warnings,
#    signals, the proc definition) and whether a termination condition is met.
python -m scripts.session evaluate --session <session.json>

# 3. DECIDE (you) — read that JSON, ground it in references/ + the decision-log
#    cache (and Microsoft Learn MCP on a cache miss), and pick ONE smallest-safe
#    change. Write it as {kind, rationale, apply_sql, rollback_sql,
#    target_object} to a small JSON file. If "stop_suggested" is true, skip to 5.

# 4. APPLY — apply your change to a fresh sandbox copy (<proc>_opt_v<n>), then
#    go back to step 2 to re-verify. The live proc is never touched.
python -m scripts.session apply --session <session.json> --change change.json

# 5. FINISH — write report.html, winner.sql, changes.sql, manifest.json.
python -m scripts.session finish --session <session.json>
```

Each step is its own process; the loop state (workload, current sandbox
variant, iteration counter, per-iteration history) round-trips through
`session.json` between commands, so you can reason freely between steps. `apply`
takes exactly one change per call (mirroring the one-change-per-iteration
discipline). Run `python -m scripts.session status --session <session.json>` at
any time to see where the run stands.

### Alternative entry point — the self-contained loop (in-process model)

When **no agent** is in the loop (unattended / CI), `scripts.optimize` runs the
whole thing in one process and calls a model itself through an `LLMBackend`:

```bash
cd sp-optimizer
python -m scripts.optimize \
  --proc "<schema>.<your_proc>" \
  --backend litellm --model "gemini/gemini-1.5-flash" \
  --max-iterations 5 \
  --target-fraction 0.8 \
  --out-dir out
# --conn is read from SQL_CONNECTION_STRING (.env) if omitted.
# --model defaults to LLM_MODEL (.env), or ollama_chat/gemma4 against a local
# Ollama server (http://localhost:11434) if neither is set.
# add --actual to capture runtime stats (executes the proc — non-prod only).
# --backend file --decisions <path> replays pre-staged agent decisions (no key).
```

Both entry points produce the identical run folder (report, run.log,
changes.sql, winner.sql, manifest.json, evidence/) — they differ only in *who
makes the decision*.

Walk through the modules in this order when reading or extending the code:
1. `scripts/discover.py` — parameter discovery + data-derived workload (see `references/parameter-discovery.md`)
2. `scripts/capture.py` — execution plan + runtime capture
3. `scripts/analyze.py` — plan XML scoring (see `references/plan-analysis.md`)
4. `scripts/session.py` — the agent-driven step commands (discover/evaluate/apply/finish); the decision step is YOURS, no model in-process
5. `scripts/optimize.py` — the self-contained orchestration loop + in-process LLM decision step
6. `scripts/llm.py` — pluggable in-process LLM backend used only by `optimize.py` (any provider via LiteLLM, or `FileBackend` replay); the decision prompt encodes the index discipline from `references/indexing-best-practices.md`

## Decision grounding (Microsoft Learn MCP, cached)

When the decision step (step 4) is driven by you, the agent — the
`python -m scripts.session` flow above, or the legacy `--backend file` replay —
ground every proposed change in official guidance, but **check the cache before
paying for a fresh lookup**:

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

## Who makes the decision (step 4)

The decision step is the only place judgement is required; every other step is deterministic and needs no model. There are two ways to supply it:

- **Agent-driven (`python -m scripts.session`)** — recommended when a coding agent is running the skill. *You* make the decision between `evaluate` and `apply`; there is no `LLMBackend`, no LiteLLM import, and no API key in the process. This is the flow in "Entry point — YOU (the agent) drive the steps" above.
- **In-process backend (`scripts.optimize`)** — for unattended / CI runs with no agent. `scripts/llm.py` exposes a `propose_change(context) -> Change` interface with two implementations: `LiteLLMBackend` (default; routes to any provider — local Ollama, OpenAI, Anthropic, Gemini, Azure, Bedrock, etc. — via `LLM_MODEL` / `--model` + the matching API key; defaults to `ollama_chat/gemma4` against `http://localhost:11434` when unset) and `FileBackend` (`--backend file --decisions <path>`), which replays pre-staged agent decisions from JSON when no API key is available.

The structured JSON prompt that drives the decision step lives in `scripts/llm.py` (`SYSTEM_PROMPT`) — it asks for a single, smallest-safe change plus rationale and a rollback, returned as strict JSON. None of it references any specific procedure, so it applies unchanged to whatever proc you target.

## Output

Every run gets its **own folder**, namespaced by procedure and timestamp, so
runs never collide and each is fully self-contained:

```
out/<schema.proc>/<YYYY-MM-DD_HHMMSS>/
  report.html      self-contained HTML report (links into evidence/)
  run.log          structured, timestamped step-by-step log of the whole loop
  changes.sql      every applied change + its rollback, in order
  winner.sql       the best-performing procedure variant + the changes that produced it
  manifest.json    machine-readable index of every iteration, combo, and evidence file
  evidence/
    iter<n>/
      <combo>.plan.xml        the execution plan captured for that combo
      <combo>.statistics.txt  SET STATISTICS IO/TIME text (actual mode)
      <combo>.score.json      the deterministic analysis: score, warnings, signals
```

The base directory is `out` by default; override with `--out-dir`. The report
path can be overridden with `--report` (defaults to `report.html` inside the
run folder).

**Evidence capture is the contract:** every plan and IO stat captured at every
step is written to disk under `evidence/` and **referenced from the report** —
the per-combo table links straight to each plan XML and IO-stat file, and an
"Evidence & artifacts" section lists the run folder and every artifact. Nothing
the loop reasoned over is left only in memory.

- A self-contained HTML report (`report.html`): a baseline-vs-final summary, a "what was tried" timeline, per-iteration workload scores with the change applied (what + why) and its apply/rollback SQL, **per-combo links to the raw plan/IO-stat evidence**, and an artifacts index.
- A `changes.sql` file with every applied change and its rollback.
- A `winner.sql` containing the best-performing procedure variant.
- A `run.log` with a timestamped trace of discover → capture → analyze → decide → apply for every iteration.
- A `manifest.json` indexing every iteration, combo, score, and evidence path.

## Examples

`examples/worldwideimporters/` holds one fully worked run (a covering index + `OPTION (RECOMPILE)` on `Integration.GetMovementUpdates`). It is illustrative only — useful to see the shape of `combos.json`, `decisions.json`, and `winner.sql` — and is **not** required to run the skill against your own proc.

## Extending

- New plan-analysis rules go in `scripts/analyze.py` and are documented in `references/plan-analysis.md`.
- To add a parameter-value strategy (e.g. pull real distributions from Query Store or a stats histogram, or map more predicate shapes), extend `scripts/discover.py` — `derive_combos_from_data()` is the data-anchored generator that keeps the workload generic across procs.
- Indexing guidance the LLM must follow when proposing an index lives in `references/indexing-best-practices.md`; keep the `SYSTEM_PROMPT` in `scripts/llm.py` in sync with it.
- Every Microsoft Learn MCP lookup made during a decision step should leave a new entry in `references/decision-log.md` (format documented at the top of that file) so the next run can reuse it instead of re-querying.
