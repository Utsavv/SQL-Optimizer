# sp-optimizer

An AI **skill** that iteratively optimizes SQL Server stored procedures by
running a closed feedback loop: discover the parameter space → capture
execution plans across it → analyze them deterministically → let an LLM propose
one smallest-safe change → apply it to a **sandbox copy** → re-verify → repeat
until the *majority* of parameter calls land on a good plan.

It directly targets **parameter sniffing**, where a proc compiled for one set of
arguments performs badly for others.

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
`SKILL.md` for the full operating procedure, termination conditions, and the
non-negotiable safety rules.

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
| `scripts/discover.py` | Discover | parameter space → workload combos, **auto-derived from the proc's real data** (`derive_combos_from_data`) | no |
| `scripts/capture.py` | Capture | execution plan + runtime capture | no |
| `scripts/analyze.py` | Analyze | deterministic plan-XML scoring | no |
| `scripts/llm.py` | Decide | propose one safe change as strict JSON; pluggable LiteLLM / file backend | **yes** |
| `scripts/optimize.py` | Apply + Verify | the loop + sandbox management + CLI + report | no |

The skill walks these in order — discover → capture → analyze → decide →
apply → verify — and loops until a termination condition in `SKILL.md` is met.

### Running the engine directly

The skill normally invokes this for you, but the same pipeline runs as a CLI when
you want to drive it by hand or in CI:

```bash
python -m scripts.optimize \
  --proc "dbo.usp_GetMemberActivity" \
  --conn "Driver={ODBC Driver 18 for SQL Server};Server=.;Database=Loyalty;Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes" \
  --backend litellm --model "gemini/gemini-1.5-flash" \
  --max-iterations 5 --target-fraction 0.8 \
  --report out/report.html
# --conn is read from SQL_CONNECTION_STRING (.env) if omitted.
# --model defaults to LLM_MODEL (.env) if omitted.
# Add --actual to capture runtime stats (executes the proc — non-prod only).
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

`scripts/llm.py`'s `LiteLLMBackend` reads `LLM_MODEL` from `.env` by default,
or pass `--model` on the CLI to override per run.

## Why this is different from existing tools

Existing tools (PerformanceStudio, PerformanceMonitor, SQL MCP Server) do
one-shot plan analysis or monitoring. None of them run an autonomous
**discover → change → re-verify** loop scored across the whole parameter
workload. That loop — driven by the skill — is the contribution here.

## Safety

- The live procedure is **never** modified — changes go to `<proc>_opt_v<n>`.
- Estimated plans are read-only; actual execution is opt-in (non-prod only).
- Every change carries a rollback; all changes are written to `changes.sql`.
- Indexes are never auto-created on production without surfacing cost/space
  impact and getting confirmation.

## Output

A run produces, under the run's output dir:

- A self-contained HTML report (`report.html`): a baseline-vs-final summary, a
  "what was tried" timeline, and per-iteration workload scores with the change
  applied (what + why) and its apply/rollback SQL.
- A `changes.sql` with every applied change and its rollback.
- A `winner.sql` containing the best-performing procedure variant.

`examples/worldwideimporters/` holds one fully worked run for reference
(a covering index + `OPTION (RECOMPILE)`). It's illustrative only — not required
to run the skill against your own proc.

## Status

Scaffold / v0. The deterministic pipeline (discover + analyze) is tested
offline, including the data-derived workload generator (`derive_combos_from_data`)
that makes the skill generic across procedures. The DB-facing steps need a live
SQL Server to exercise. Mining concrete argument values from Query Store and
mapping more predicate shapes (multi-column, function-wrapped) are the next
enhancements.
</content>
</invoke>
