# Decision Log — cached Microsoft Learn grounding

A running cache of SQL Server guidance already looked up via the Microsoft
Learn MCP (`microsoft_docs_search` / `microsoft_docs_fetch` /
`microsoft_code_sample_search`) during past decision steps (step 4 of the
core loop in `SKILL.md`).

**Purpose:** avoid re-spending tokens on MCP search + fetch calls for a
question this skill has already answered. Microsoft Learn doc fetches are
the most expensive part of the decision step — a cache hit here costs a
grep, a miss costs a full search-and-fetch round trip.

## How to use this file (decision step, step 4)

1. **Read this file first.** Match the current finding (warning type, signal,
   or proposed change `kind`) against the `Keywords:` line of each entry
   below.
2. **If an entry covers the question** — its `Takeaway:` directly answers
   what change to propose, or rules one out — use it. Do not call Microsoft
   Learn MCP. Cite the entry's `Source:` URL(s) in the rationale, same as if
   you had just fetched them.
3. **If no entry covers it, or the entry is for a meaningfully different
   scenario** (different warning type, different SQL Server version /
   engine, conflicting signal) — call Microsoft Learn MCP as normal, then
   **append a new entry** below in the same format before finishing the
   decision step.
4. **Don't duplicate.** If a new MCP query lands on the same takeaway as an
   existing entry, add the new keywords/query to that entry instead of
   creating a near-duplicate one.
5. Entries are proc-agnostic SQL Server knowledge — write them so they apply
   to any procedure, not just the one being tuned when the entry was added.

## Entry format

```
## Topic: <short-kebab-case-id>
Keywords: <comma-separated terms the agent would match a finding against>
Q: <the question this entry answers>
Takeaway: <the actionable guidance, 1-3 sentences>
Source: <cited Microsoft Learn URL(s)>
```

---

## Topic: covering-index-date-range-filter
Keywords: covering index, INCLUDE columns, nonclustered index, missing index,
table scan, SARGable date range, key lookup
Q: When a proc scans a large table on a date-range predicate with no
supporting index, what index should be proposed?
Takeaway: Create a nonclustered index keyed on the filtered column(s)
(equality predicates before range predicates), with the SELECT-list columns
not already in the key added via `INCLUDE`. The clustered index key is
automatically included in every nonclustered index, so it doesn't need to be
restated. Only propose this when the analysis shows a missing-index signal,
a key lookup, or a scan over a large input — never speculatively.
Source:
https://learn.microsoft.com/sql/relational-databases/sql-server-index-design-guide?view=sql-server-ver17#nonclustered-index-design-guidelines
https://learn.microsoft.com/sql/relational-databases/indexes/create-indexes-with-included-columns?view=sql-server-ver17

## Topic: sargability-date-range-predicate
Keywords: SARGable, function-wrapped predicate, implicit conversion, index
seek vs scan, predicate not sargable
Q: Is a predicate of the shape `Col > @A AND Col <= @B` (column alone on one
side, no function wrapping) SARGable?
Takeaway: Yes — a bare column compared to a parameter with no function or
implicit conversion is already SARGable. If a scan still occurs, the gap is
the missing supporting index, not the predicate shape, so don't propose a
rewrite — propose the index instead (see
[[covering-index-date-range-filter]] above).
Source:
https://learn.microsoft.com/troubleshoot/sql/database-engine/performance/troubleshoot-high-cpu-usage-issues#step-6-investigate-and-resolve-sargability-issues

## Topic: parameter-sniffing-recompile
Keywords: parameter sniffing, OPTION RECOMPILE, OPTIMIZE FOR UNKNOWN, memory
grant skew, sort spill tempdb, plan reuse, cached plan wrong rows
Q: A cached plan compiled for one parameter value's row count (e.g. a narrow
window) gets reused for a much wider call, causing a memory-grant-driven
spill (e.g. a sort spilling to tempdb) because the grant was sized for the
first compile, not the current call. What's the fix?
Takeaway: Add `OPTION (RECOMPILE)` to the statement so the optimizer
generates a plan (and right-sizes the memory grant) for each call's actual
parameter values. This is the right tradeoff when the proc has highly
variable/skewed input across calls and isn't invoked in a tight, high-volume
loop — the per-call compile cost is cheap relative to the misestimate it
removes. If the proc IS called at high frequency, consider `OPTIMIZE FOR
UNKNOWN`/specific value hints instead, which avoid the per-call recompile
cost but don't right-size per call the way `RECOMPILE` does.
Source:
https://learn.microsoft.com/sql/relational-databases/query-processing-architecture-guide?view=sql-server-ver17#execution-plan-caching-and-reuse
https://learn.microsoft.com/sql/relational-databases/post-migration-validation-and-optimization-guide?view=sql-server-ver17#sensitivity-to-parameter-sniffing
https://learn.microsoft.com/sql/relational-databases/memory-management-architecture-guide?view=sql-server-ver17#effects-of-min-memory-per-query
