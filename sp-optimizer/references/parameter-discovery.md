# Parameter Discovery

`scripts/discover.py` builds the "workload" — the set of parameter value
combinations the proc is tested against. The quality of optimization depends
heavily on how representative this workload is.

## Three sources, in priority order

1. **Signature** (`sys.parameters`) — always available. Gives names and types.
2. **Query Store** — if enabled, mines recently-executed call texts so you tune
   against values that *actually occurred* in production traffic. This is the
   single biggest lever for realistic results. Currently the raw texts are
   surfaced for the LLM/caller to parse; a future enhancement is to extract
   concrete argument values automatically.
3. **Synthesized** — boundary + typical values per type family. Guarantees a
   workload even with no Query Store. Datetime params get an old-vs-recent pair
   because date ranges are the most common sniffing trigger in reporting procs.

## Why date params get special treatment

A proc compiled for `@FromDate = '2024-06-01'` (narrow, recent range → seek)
often performs terribly when called with `@FromDate = '2020-01-01'` (wide range
→ should scan). This asymmetry is the textbook sniffing case, so the synthesizer
always includes both an old and a recent date to force the divergent plans into
the workload where the analyzer can see them.

## Weighting

Each `ParamCombo` carries a `weight` (default 1.0). When you mine real values
from Query Store, set the weight to the observed call frequency so the aggregate
workload score reflects real traffic, not uniform assumptions.

## Combinatorial explosion

The synthesizer takes the cartesian product of per-param candidate values, so
combos grow fast. `max_combos` caps the total (default 12). For wide procs,
prefer Query Store mining over synthesis, or sample the product rather than
taking the first N.
