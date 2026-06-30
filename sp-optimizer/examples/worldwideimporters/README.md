# Worked example — WideWorldImporters

These files are a **concrete example** of one optimizer run, kept for reference.
They are **not** required to run the skill and are **not** specific to how the
skill operates — point the optimizer at any procedure in any database and it
generates its own equivalents.

| File | What it is |
|---|---|
| `combos.json` | The workload (parameter windows) for `Integration.GetMovementUpdates`. Today the skill **derives this automatically** from the proc's real column range (see `scripts/discover.py`); this file shows the shape you'd hand-write only if you want to override that. |
| `decisions.json` | The two changes the agent proposed for this proc (covering index, then `OPTION (RECOMPILE)`), in the `FileBackend` replay format. |
| `winner.sql` | The recommended production changes that came out of the run. |
| `RUN-LOG.md` | The narrative behind these artifacts: env setup, target selection, debugging attempts, and the before/after numbers (aggregate plan score 73.0 → 98.8, logical reads 2,401 → 7). |

## Reproducing the shape for your own proc

You don't need any of these to start. The generic path is just:

```bash
python -m scripts.optimize --proc "<your.proc>" --backend claude
```

The workload is derived from your proc's data automatically. Supply a
`combos.json` (via `SP_OPT_COMBOS`) or a `decisions.json` (via
`--backend file --decisions`) **only** when you want to override the
auto-derived workload or replay agent-made decisions without a live LLM call.
