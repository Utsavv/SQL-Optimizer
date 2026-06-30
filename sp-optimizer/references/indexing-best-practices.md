# Indexing Best Practices

When the optimizer proposes a `kind="index"` change (step 4), it must follow the
discipline below. The cost asymmetry is the governing principle: a *missing*
index is a slow query you can fix later, but an *extra* index is a permanent
write tax on every INSERT/UPDATE/DELETE. On large tables, bias toward fewer,
well-targeted indexes over broad speculative coverage.

## Build from evidence, never speculation

Propose an index only when the workload analysis justifies it — a missing-index
signal from the plan, a key-lookup or scan over a large input, or a confirmed
predicate that has no supporting index. Don't add indexes defensively. Verify
demand against the DMVs first:

```sql
-- missing index candidates (rank by impact)
SELECT
    mid.statement AS table_name,
    mid.equality_columns,
    mid.inequality_columns,
    mid.included_columns,
    migs.user_seeks,
    migs.avg_user_impact
FROM sys.dm_db_missing_index_details mid
JOIN sys.dm_db_missing_index_groups mig ON mid.index_handle = mig.index_handle
JOIN sys.dm_db_missing_index_group_stats migs ON mig.index_group_handle = migs.group_handle
ORDER BY migs.avg_user_impact DESC;
```

The missing-index DMVs are a *starting point*, not a spec — they suggest columns
but ignore ordering, existing indexes, and write cost. Treat their output as a
hint to be refined, not a CREATE INDEX statement to paste.

## Nonclustered key column order

1. **Equality predicates before range predicates.** Columns used with `=` come
   first; columns used with `>`, `<`, `BETWEEN`, `LIKE 'x%'` come last. A range
   column in the middle of the key stops the index from seeking on anything
   after it.
2. **Within the equality columns, order by selectivity** (most selective first)
   so the seek narrows fastest.
3. **`INCLUDE` columns needed only in the SELECT list** to make the index
   covering and avoid key lookups — but don't bloat it. Every included column
   adds write cost and storage. Include only what the covered queries actually
   project.

Index for the predicate first, the SELECT list second.

## Avoid duplication and overlap

Before creating an index, check whether an existing index already covers the
need — particularly a left-prefix subset (an existing key `(A, B)` already
serves a proposed key `(A)`; prefer extending or `INCLUDE` over adding a new,
overlapping index). Surface redundant/unused indexes rather than stacking
another on top:

```sql
-- unused indexes: high writes, no reads (candidates to drop, not add to)
SELECT
    OBJECT_NAME(s.object_id) AS table_name,
    i.name AS index_name,
    s.user_seeks, s.user_scans, s.user_lookups, s.user_updates
FROM sys.dm_db_index_usage_stats s
JOIN sys.indexes i ON s.object_id = i.object_id AND s.index_id = i.index_id
WHERE s.database_id = DB_ID()
    AND (s.user_seeks + s.user_scans + s.user_lookups) = 0
    AND s.user_updates > 0
ORDER BY s.user_updates DESC;
```

## Clustered index (only when redesigning the table)

This skill rarely changes clustering, but if it proposes one: pick a narrow,
static, unique, ever-increasing key — IDENTITY int/bigint over GUID for OLTP,
since GUIDs cause random insert points and page splits. The clustering key is a
**separate decision from the PK**; don't default to "PK = clustering key"
without weighing range-query patterns and access paths.

## Filtered and columnstore — situational only

- **Filtered index** for a narrow, frequently queried subset of a large table
  (e.g. active rows out of a mostly-archived table). Good fit when the proc's
  predicate consistently selects the same slice.
- **Columnstore** for analytical/reporting workloads over large, mostly-read
  fact tables — not for high-frequency transactional access.

## Statistics, not just structure

Plan quality depends on current statistics as much as on index structure. Rely
on `AUTO_UPDATE_STATISTICS`, and recommend a manual `UPDATE STATISTICS` on
high-churn or large tables where the default sampling rate misses drift — this
is often the cheaper fix than a new index when the plan is bad due to stale
estimates (visible as estimate-vs-actual skew in the analysis).

## Fragmentation maintenance (advisory, not a default action)

Check actual fragmentation before recommending any rebuild; never blind-schedule
one.

```sql
SELECT
    OBJECT_NAME(ips.object_id) AS table_name,
    i.name AS index_name,
    ips.avg_fragmentation_in_percent,
    ips.page_count
FROM sys.dm_db_index_physical_stats(DB_ID(), NULL, NULL, NULL, 'LIMITED') ips
JOIN sys.indexes i ON ips.object_id = i.object_id AND ips.index_id = i.index_id
WHERE ips.page_count > 1000
ORDER BY ips.avg_fragmentation_in_percent DESC;
```

Rough thresholds: under 10% leave alone, 10–30% `REORGANIZE`, above 30%
`REBUILD`. These matter less on SSD-backed storage than they used to — treat
them as a starting point, weigh I/O pattern and table size, and don't commit to
a maintenance cadence without that context.

## Checklist before emitting a `kind="index"` change

- [ ] Backed by an actual plan/DMV signal, not speculation.
- [ ] No existing index (or left-prefix of one) already covers it.
- [ ] Key columns ordered: equality before range, most-selective equality first.
- [ ] `INCLUDE` limited to columns the covered queries project.
- [ ] Write-cost / storage impact stated in the rationale (per the skill's
      "never auto-create indexes on production without surfacing cost" rule).
- [ ] Considered whether updated statistics or a hint would fix it more cheaply.
