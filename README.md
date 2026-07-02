# SQL-Optimizer

An AI-driven toolkit for tuning SQL Server stored procedures. The core of the
repo is **sp-optimizer**, a [Claude Code skill](https://code.claude.com/docs)
that runs a closed discover → capture → analyze → decide → apply → verify loop
against a procedure's real parameter space until it lands on a good plan
across the majority of realistic calls — not just one lucky compile. The rest
of the repo is supporting tooling: scripts to stand up a SQL Server test
database, verify connectivity, and roll back everything an optimizer run
created.

It directly targets **parameter sniffing**, where a proc compiled for one set
of arguments performs badly for others.

## Repository layout

| Path | What it is |
|---|---|
| `sp-optimizer/` | The optimizer itself: `SKILL.md` (the skill definition), `scripts/` (the Python engine), `references/` (grounding docs used at the decision step), `examples/worldwideimporters/` (one worked run, including `RUN-LOG.md`), `out/` (per-run output, git-ignored) |
| `.claude/skills/sp-optimizer` | Symlink into `sp-optimizer/` so Claude Code auto-discovers the skill from this repo |
| `workload-drivers/` | Headless load generators that put a heavy concurrent OLTP workload on WideWorldImporters (order-entry and vehicle-location inserts), so you can assess stored-procedure performance *under load*. Cross-platform Python ports of Microsoft's `wide-world-importers/workload-drivers` sample |
| `setup/` | One-time environment bootstrap scripts — provision/load a test database, verify connectivity, roll back an optimizer run. Not needed if you already have a SQL Server to point the optimizer at |
| `setup/deploy_wwi_free.sh` | Provisions a **new** Azure SQL Database (free tier) and loads the WorldWideImporters sample DB into it |
| `setup/deploy_wwi_existing.sh` | Loads WorldWideImporters into an **existing** Azure SQL Server via `sqlpackage` |
| `setup/test_connection.py` | Quick connectivity check against `SQL_CONNECTION_STRING` from `.env` |
| `setup/CLEANUP.sql` | Rollback script — drops every sandbox object (`<proc>_opt_v<n>` clones, added indexes) an optimizer run created |
| `requirements.txt` | Python deps: `pyodbc`, `python-dotenv`, `litellm` |
| `.env.example` | Template for `SQL_CONNECTION_STRING` and the LLM backend config — copy to `.env` and fill in |

## Quick start

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```
   Requires ODBC Driver 18 for SQL Server. Works against on-prem SQL Server
   (2016+), Azure SQL MI, and AWS RDS for SQL Server.

2. **Configure connection + LLM backend**
   ```bash
   cp .env.example .env
   # fill in SQL_CONNECTION_STRING, LLM_MODEL, and the matching API key
   ```
   `.env` is git-ignored; `.env.example` is the safe-to-commit template.

3. **(Optional) Stand up a test database.** Skip this if you already have a
   SQL Server to point the optimizer at.
   - `setup/deploy_wwi_free.sh` — provisions a brand-new Azure SQL Database (free
     tier) and imports WorldWideImporters into it. Auto-installs the Azure CLI
     via Homebrew if missing; requires an Azure login and `SQL_ADMIN_PASSWORD`.
     ```bash
     export SQL_ADMIN_PASSWORD='YourStr0ngP@ssword!'
     bash setup/deploy_wwi_free.sh
     ```
   - `setup/deploy_wwi_existing.sh` — imports WorldWideImporters into an
     already-provisioned Azure SQL Server via `sqlpackage` (auto-installed via
     `dotnet tool` if missing). Edit the `SQL_SERVER`/`SQL_DB`/`SQL_ADMIN`
     constants at the top of the script to match your server, and set
     `SQL_ADMIN_PASSWORD` rather than relying on the script default.
     ```bash
     export SQL_ADMIN_PASSWORD='YourStr0ngP@ssword!'
     bash setup/deploy_wwi_existing.sh
     ```
   Both scripts print a ready-to-use pyodbc connection string and an example
   optimizer invocation when they finish.

4. **Verify connectivity**
   ```bash
   python setup/test_connection.py
   ```
   Connects with `SQL_CONNECTION_STRING` and prints the server version and
   database name on success.

5. **Run the optimizer** — either ask Claude Code to use the `sp-optimizer`
   skill in plain language, or drive the engine directly via its CLI. See
   [sp-optimizer](#sp-optimizer) below.

6. **Clean up.** `setup/CLEANUP.sql` drops every sandbox proc/index an optimizer run
   created, returning the database to its pre-run state. Adjust the object
   names at the top of the script to match what your run actually produced
   (it's written for the `Integration.GetMovementUpdates` example by default).

---

# sp-optimizer

An AI **skill** that iteratively optimizes SQL Server stored procedures by
running a closed feedback loop: discover the parameter space → capture
execution plans across it → analyze them deterministically → let an LLM propose
one smallest-safe change → apply it to a **sandbox copy** → re-verify → repeat
until the *majority* of parameter calls land on a good plan.

## What this is

This is a [Claude Code skill](https://code.claude.com/docs) (`SKILL.md`),
not just a script. You drive it in plain language and it orchestrates the loop
for you, calling the Python modules in this repo at each step. The Python is the
**engine**; the skill is the **driver** that decides when to discover, capture,
analyze, change, and re-verify.

## Using the skill

Point your agent at the repo and ask it, in plain language, to tune a procedure:

> "Optimize `dbo.usp_GetMemberActivity` in my Loyalty database — it's slow for
> some date ranges."

The skill takes over from there. It will:

1. **Discover** the proc's parameters and derive a representative workload
   **from the proc's own data** (no hand-written test cases).
2. **Capture** an execution plan for each parameter combination.
3. **Analyze** those plans deterministically — scans, spills, sniffing skew,
   missing indexes.
4. **Decide** on a single smallest-safe change (hint, `OPTION`, index, rewrite)
   via the LLM, or stop if the proc is already good enough.
5. **Apply** the change to a **sandbox copy** (`<proc>_opt_v<n>`) — never the
   live object.
6. **Verify** by re-capturing plans across the *same* workload, and repeat until
   the majority improve without regressions.

The skill is **procedure-agnostic**: just name a different proc. The workload is
derived from each proc's real column ranges, so there's no per-proc setup. See
`sp-optimizer/SKILL.md` for the full operating procedure, termination
conditions, and the non-negotiable safety rules.

### Talking to the skill

You don't need flags to use the skill — describe the goal and let it run. The
common levers it exposes:

- **Which proc** — any schema-qualified name (`dbo.usp_GetMemberActivity`).
- **How aggressive** — a target fraction of combos that must reach a good plan
  (default 80%) and a max iteration count (default 5).
- **Estimated vs actual** — estimated plans are read-only and the default; ask
  for *actual* runtime stats only against non-prod, since that executes the proc.
- **Which LLM backend** — any provider via [LiteLLM](https://docs.litellm.ai/docs/providers) (OpenAI, Anthropic, Gemini, Azure, Bedrock, ...), or a replay/file backend.

## How the Python code powers the skill

The skill orchestrates; these Python modules do the deterministic work. The LLM
is only consulted at the single decision step — everything else is testable
offline.

| Module | Step it serves | Role | LLM? |
|---|---|---|---|
| `scripts/discover.py` | Discover | parameter space → workload combos: **real Query Store call values** blended with combos **auto-derived from the proc's real data** (`derive_combos_from_data`) | no |
| `scripts/review.py` | Review | deterministic T-SQL lint (SARGability, catch-alls, NOLOCK, …) + param/column type mismatch via `sys.columns`; findings feed the report and the decision prompt | no |
| `scripts/capture.py` | Capture | execution plan + runtime capture (median of `--runs` executions, session wait profile) | no |
| `scripts/analyze.py` | Analyze | deterministic plan-XML scoring (scans, missing indexes, spills, memory grants, spools, scalar UDFs, compiled-vs-runtime sniffing) | no |
| `scripts/guardrails.py` | Apply | pre-apply index checks: overlap rejection against existing indexes, size estimate, write-tax evidence | no |
| `scripts/llm.py` | Decide | propose one safe change as strict JSON; pluggable LiteLLM / file backend | **yes** |
| `scripts/optimize.py` | Apply + Verify | the loop + sandbox management + per-combo regression gate + rollbacks + CLI + report | no |
| `scripts/evidence.py` | Capture + Verify | writes per-combo plan XML / IO stats / score JSON into each run's `evidence/` folder | no |
| `scripts/simulate.py` | Validate | proc-shaped load simulator + paired under-load A/B (baseline vs winner) with wait profiles | no |
| `scripts/models.py` | all steps | shared dataclasses for combos, plans, scores, decisions | no |

Offline test suite (no SQL Server needed): `cd sp-optimizer && python -m pytest tests`.

The skill walks these in order — discover → capture → analyze → decide →
apply → verify — and loops until a termination condition in `SKILL.md` is met.

### Running the engine directly

The skill normally invokes this for you, but the same pipeline runs as a CLI when
you want to drive it by hand or in CI:

```bash
cd sp-optimizer
python -m scripts.optimize \
  --proc "dbo.usp_GetMemberActivity" \
  --conn "Driver={ODBC Driver 18 for SQL Server};Server=.;Database=Loyalty;Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes" \
  --backend litellm --model "gemini/gemini-1.5-flash" \
  --max-iterations 5 --target-fraction 0.8 \
  --out-dir out
# Each run lands in out/<schema.proc>/<timestamp>/ (report, run.log, changes.sql,
# winner.sql, manifest.json, and evidence/). Override the report path with --report.
# --conn is read from SQL_CONNECTION_STRING (.env) if omitted.
# --model defaults to LLM_MODEL (.env) if omitted.
# Add --actual to capture runtime stats (executes the proc — non-prod only),
# --runs 3 for median-of-3 timings, --regression-tolerance for the rollback
# gate, --allow-plan-forcing to enable Query Store plan forcing proposals,
# and --no-auto-rollback to keep losing changes at end of run.

# Validate the winner under load afterwards (paired A/B + wait profile):
python -m scripts.simulate --proc "dbo.usp_GetMemberActivity" \
  --compare-proc "dbo.usp_GetMemberActivity_opt_v2" --threads 8 --duration 120
```

## Install

```bash
pip install -r requirements.txt
```

Requires ODBC Driver 18 for SQL Server. Works against on-prem SQL Server
(2016+), Azure SQL MI, and AWS RDS for SQL Server.

### LLM backend (LiteLLM)

The decision step calls the model through [LiteLLM](https://docs.litellm.ai/docs/providers),
so switching providers is a config change, not a code change: set `LLM_MODEL`
to a LiteLLM model string in `.env` and put the matching API key alongside it.

```python
from litellm import completion
import os

# Set your API key(s) in environment variables (or .env — see .env.example)
os.environ["GEMINI_API_KEY"] = "your-gemini-key"
os.environ["OPENAI_API_KEY"] = "your-openai-key"
os.environ["ANTHROPIC_API_KEY"] = "your-anthropic-key"

messages = [{"content": "Hello, how are you?", "role": "user"}]

# To use Gemini
response = completion(model="gemini/gemini-1.5-flash", messages=messages)

# To switch provider, you ONLY change the model string:
response = completion(model="gpt-4o", messages=messages)
response = completion(model="claude-3-5-sonnet-20241022", messages=messages)

print(response.choices[0].message.content)
```

| Provider | `LLM_MODEL` example | API key env var |
|---|---|---|
| Gemini | `gemini/gemini-1.5-flash` | `GEMINI_API_KEY` |
| OpenAI | `gpt-4o` | `OPENAI_API_KEY` |
| Anthropic | `claude-3-5-sonnet-20241022` | `ANTHROPIC_API_KEY` |
| Ollama (local) | `ollama/<model>` | none — local server |

`scripts/llm.py`'s `LiteLLMBackend` reads `LLM_MODEL` from `.env` by default,
or pass `--model` on the CLI to override per run.

#### API key vs. Claude/ChatGPT subscription

`LiteLLMBackend` calls each provider's **developer API** (`api.anthropic.com`,
`api.openai.com`, ...), which is billed per token and authenticated with an
**API key** — *not* a consumer **Claude Pro/Max** or **ChatGPT Plus/Pro**
subscription. Those subscriptions power the chat products (claude.ai,
chatgpt.com) and don't expose a programmatic endpoint LiteLLM can call, so they
can't be dropped in where an API key is expected.

To drive the decision step from a subscription instead of a paid API key, use
the **`FileBackend`** path (no API key required):

1. Run an external agent under your subscription — e.g. **Claude Code**, which
   supports Claude Pro/Max login — and have it make the "propose one
   smallest-safe change" decisions.
2. Write those decisions to a JSON file in the same shape `LiteLLMBackend`
   emits (a JSON array of `{kind, rationale, apply_sql, rollback_sql,
   target_object}` objects).
3. Point the loop at it: `--backend file --decisions <path>`. `FileBackend`
   replays each staged decision in order — zero per-token API billing.

In short: in-process LLM call → needs an API key (`LiteLLMBackend`); external
subscription-backed agent → no key (`FileBackend`).

## Why this is different from existing tools

Existing tools (PerformanceStudio, PerformanceMonitor, SQL MCP Server) do
one-shot plan analysis or monitoring. None of them run an autonomous
**discover → change → re-verify** loop scored across the whole parameter
workload. That loop — driven by the skill — is the contribution here.

## Safety

- The live procedure is **never** modified — changes go to `<proc>_opt_v<n>`,
  and `drop_sandbox` refuses to touch any object not named `*_opt_v<n>`.
- Estimated plans are read-only; actual execution is opt-in (non-prod only).
- Every change carries a rollback; all changes are written to `changes.sql` —
  and rollbacks are **executed** automatically when a change regresses any
  combo beyond `--regression-tolerance` or is not part of the winning variant
  at end of run (`--no-auto-rollback` opts out).
- Proposed indexes pass deterministic guardrails before creation: overlap
  with an existing index is rejected outright, and an estimated size +
  write-tax note is logged for every index that is allowed.
- Query Store plan forcing changes live behavior, so it is only offered to
  the decision step behind `--allow-plan-forcing`.
- `setup/CLEANUP.sql` gives you a known-good rollback path for whatever
  a run leaves behind.

## Output

Every run gets its **own folder**, namespaced by procedure and timestamp, so
runs never collide and each run is fully self-contained:

```
out/<schema.proc>/<YYYY-MM-DD_HHMMSS>/
  report.html      self-contained HTML report (links into evidence/)
  run.log          timestamped, step-by-step trace of the whole loop
  changes.sql      every applied change + its rollback, in order
  winner.sql       the best-performing procedure variant + the changes that produced it
  manifest.json    machine-readable index of every iteration, combo, and evidence file
  evidence/
    iter<n>/
      <combo>.plan.xml        execution plan captured for that combo
      <combo>.statistics.txt  SET STATISTICS IO/TIME text (actual mode)
      <combo>.score.json      deterministic analysis: score, warnings, signals
```

**Every piece of evidence captured at every step** — the execution plan and the
IO statistics for each parameter combo, at each iteration — is written under
`evidence/` and **referenced from the report**: the per-combo tables link
straight to each plan XML and IO-stat file, and an "Evidence & artifacts"
section indexes the run folder. Nothing the loop reasoned over is left only in
memory.

`sp-optimizer/examples/worldwideimporters/` holds one fully worked run for
reference (a covering index + `OPTION (RECOMPILE)`). It's illustrative only —
not required to run the skill against your own proc.

## Reference material

- `sp-optimizer/references/` — grounding docs the LLM decision step is checked
  against: `indexing-best-practices.md`, `plan-analysis.md`,
  `parameter-discovery.md`, `decision-log.md` (a reusable cache of Microsoft
  Learn citations, keyed by finding/warning type, so repeat decisions don't
  re-spend tokens on the same MCP lookup).
- `sp-optimizer/examples/worldwideimporters/RUN-LOG.md` — a complete worked
  example: optimizing `Integration.GetMovementUpdates` against
  WorldWideImporters on Azure SQL, including environment setup, target
  selection, the debugging history, and the final numbers (aggregate plan
  score 73.0 → 98.8, logical reads on the narrow 1-day incremental pull
  2,401 → 7).

## Status

The deterministic pipeline is covered by an offline pytest suite
(`sp-optimizer/tests/`, no SQL Server needed): the loop's regression gate and
rollback behavior, the T-SQL review rules, the plan-analyzer rules, the index
guardrails, Query Store combo mining, and the simulator/replay helpers. The
DB-facing paths (capture, sandbox DDL, wait-stats DMVs, XE ring buffer) need a
live SQL Server to exercise end-to-end. Mapping more predicate shapes
(multi-column, function-wrapped) in `discover.py` is the next enhancement.
</content>
