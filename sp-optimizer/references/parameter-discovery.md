# Parameter Discovery

`scripts/discover.py` builds the "workload" — the set of parameter value
combinations the proc is tested against. The quality of optimization depends
heavily on how representative this workload is.

## Sources, in priority order

1. **`SP_OPT_COMBOS` file** — an explicit, hand-curated workload wins over
   everything else when the env var points at a JSON file.
2. **Signature** (`sys.parameters`) — always available. Gives names, types, and
   (now) `has_default_value` / `default_value`, which flag *optional* params.
3. **Data-derived** — the generic engine (`derive_combos_from_data`). It reads
   the proc body, maps each parameter to the column it filters, and mines that
   column's **actual contents** to build the workload (see below). This is the
   default for any proc and the biggest lever for realistic results without
   Query Store.
4. **Query Store** — if enabled, `values_from_query_store` surfaces recently
   executed call texts so a caller/LLM can tune against values that *actually
   occurred* in production. Raw texts are surfaced for parsing today.
5. **Synthesized** — boundary + typical values per type family. Guarantees a
   workload even when nothing can be anchored to real data.

## Exploring the proc's own tables for real values

`derive_combos_from_data` no longer special-cases only datetime ranges — it
mines real data for **every** parameter it can map to a column:

- **Datetime range / bound filters** → the column's real `MIN`/`MAX`, fanned into
  narrow → medium → wide → empty windows. This is the classic parameter-sniffing
  spread (a plan compiled for "last 1 day" is often terrible for "full history").
- **Equality / other filters** (`col = @p`, etc.) → the column's real
  frequency distribution via `_sample_column_values`: a **hot common value**
  (`ORDER BY COUNT_BIG(*) DESC`, large result → the plan the optimizer should be
  good at) **and** a **selective rare value** (`... ASC`, small result → the plan
  that wants a seek). The equality predicate is therefore exercised with values
  that *exist in the table*, never an invented constant like `'common_value'`
  that returns nothing.

Column mapping traces aliases through `FROM`/`JOIN` (`sit.LastEditedWhen` →
`Warehouse.StockItemTransactions`) and handles both predicate orderings
(`col <op> @p` and `@p <op> col`). If a param can't be mapped, it falls back to a
single synthesized representative so the `EXEC` is still complete.

## Optional / catch-all parameters → NULL combinations

The other new axis. Many procs use the **optional parameter** pattern:

```sql
WHERE (@Status IS NULL OR o.Status = @Status)
  AND (@Region IS NULL OR o.Region = @Region)
```

When an optional param is `NULL` its filter short-circuits (no restriction);
when supplied it filters hard. Each *combination* of "which optional filters are
active" can compile to a completely different plan, so the engine enumerates
them. A param is treated as optional when EITHER:

- it declares a default in the signature (`has_default_value`), OR
- the body guards it with `@p IS NULL`, `ISNULL(@p, …)`, or `COALESCE(@p, …)`.

For *n* optional params, the non-empty NULL subsets (up to `2ⁿ − 1`) are
generated — e.g. three optional params yield `@A`, `@B`, `@C`, `@A,@B`, `@A,@C`,
`@B,@C`, `@A,@B,@C` NULL — alongside an all-supplied baseline. Combos are emitted
in a priority order so the highest-signal ones survive the `max_combos` cap:

```
baseline (all real values)
  → each optional param NULL on its own       (single-toggle, weight 1.5)
  → the rest of the datetime window sweep
  → multi-param NULL combinations             (weight 1.0)
  → selective rare-value variants
```

Because the NULL power set grows as `2ⁿ`, raise `--max-combos` when a proc has
many optional params and you want every combination captured (default 12).

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
