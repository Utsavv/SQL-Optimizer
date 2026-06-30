# SP Optimizer — Run Progress Log

Target DB: **WorldWideImport** (Azure SQL Database, EngineEdition 5), ODBC Driver 18.
Interpreter: `/Library/Frameworks/Python.framework/Versions/3.10/bin/python3` (pyodbc 5.3.0).
Password is read from `.env` at runtime and is **never** printed or written anywhere.

## Environment verification (Step 1 prerequisites)
- pyodbc 5.3.0 + `ODBC Driver 18 for SQL Server` present. ✅
- Connection to `utsavsqlserver.database.windows.net / WorldWideImport` succeeds. ✅
- `.env` already holds a proper **ODBC** connection string (no ADO.NET conversion needed). ✅
- Microsoft Learn MCP already installed (`microsoft_docs_search/fetch/code_sample_search`). ✅
- `ANTHROPIC_API_KEY` is **not** set → Python `ClaudeBackend` cannot call the API.
  Decision step is driven by the agent (me), grounded in Microsoft Learn docs, and fed to the
  loop via a new `FileBackend` (`--backend file --decisions <path>`).
- `Integration.GetLastETLCutoffTime()` does **not** exist in this DB → cutoffs derived from the
  fact table's real `LastEditedWhen` range instead.

## Target selection
Candidates (all take `@LastCutoff/@NewCutoff datetime2`):
- **Integration.GetMovementUpdates** → single table `Warehouse.StockItemTransactions`
  (236,667 rows), SARGable predicate `LastEditedWhen > @LastCutoff AND <= @NewCutoff`,
  `ORDER BY StockItemTransactionID`. **CHOSEN** — cleanest classic sniffing target and the fix is
  expressible as an index (the preferred change type).
- GetOrderUpdates / GetSaleUpdates use non-SARGable `CASE WHEN ... END > @LastCutoff` over
  multi-table joins (harder, needs rewrite).
- GetCustomerUpdates / GetStockItemUpdates are cursor-based temporal queries (complex).

Baseline facts on `Warehouse.StockItemTransactions`:
- Indexes: clustered PK on `StockItemTransactionID`; 6 single-column FK nonclustered indexes.
  **No index on `LastEditedWhen`** → every call scans all 236,667 rows.
- `LastEditedWhen` range: 2013-01-01 → 2016-05-31. Rows/yr: 2013=63,306 / 2014=68,386 /
  2015=74,451 / 2016=30,524. Last 30d=6,562, 7d=1,528, 1d=293.
- Workload combos overridden to realistic narrow/medium/wide/empty ranges within 2013–2016
  to expose parameter-sniffing skew.

---

## Decision grounding — Microsoft Learn MCP queries (per decision step)

### Iteration 1 decision: covering index on `LastEditedWhen`
MCP `microsoft_docs_search` queries run:
1. `covering nonclustered index INCLUDE columns to cover query date range filter`
2. `parameter sniffing OPTION RECOMPILE OPTIMIZE FOR UNKNOWN stored procedure`
3. `SARGable predicate index seek datetime range avoid table scan`

Cited docs:
- Index architecture & design guide — *Use included columns in nonclustered indexes* / *General
  index design guidelines*:
  https://learn.microsoft.com/sql/relational-databases/sql-server-index-design-guide?view=sql-server-ver17#nonclustered-index-design-guidelines
  — "Consider covering indexes when retrieving a small portion of a large table, and where that
  small portion is defined by a fixed predicate." Column order: filter (WHERE) first as key,
  projection columns as INCLUDE; the clustered key is auto-included in every nonclustered index.
- Create indexes with included columns:
  https://learn.microsoft.com/sql/relational-databases/indexes/create-indexes-with-included-columns?view=sql-server-ver17
- Parameter sniffing / RECOMPILE (held in reserve for iteration 2 if sniffing skew persists):
  https://learn.microsoft.com/sql/relational-databases/query-processing-architecture-guide?view=sql-server-ver17#execution-plan-caching-and-reuse
  https://learn.microsoft.com/sql/relational-databases/post-migration-validation-and-optimization-guide?view=sql-server-ver17#sensitivity-to-parameter-sniffing
- SARGability (confirms the predicate `LastEditedWhen > @LastCutoff AND <= @NewCutoff` is already
  SARGable — column alone on one side — so the gap is purely the missing index, not the predicate):
  https://learn.microsoft.com/troubleshoot/sql/database-engine/performance/troubleshoot-high-cpu-usage-issues#step-6-investigate-and-resolve-sargability-issues

**Decision:** create covering nonclustered index keyed on `LastEditedWhen`, INCLUDE the projected
non-key columns (`TransactionOccurredWhen, InvoiceID, PurchaseOrderID, Quantity, StockItemID,
CustomerID, SupplierID, TransactionTypeID`). `StockItemTransactionID` is the clustered key → auto
included. This is expressible purely as an index → the preferred change type per the skill rules.

### Iteration 2 decision: `OPTION (RECOMPILE)` on the SELECT (residual sniffing)
After the covering index, the actual plans exposed a genuine parameter-sniffing tail: the cached
seek+sort plan is compiled on the FIRST combo (narrow, 293 rows), so its memory grant is sized for
~293 rows. When that same plan is reused for the wide full-reload combos, the `ORDER BY` sort spills
to tempdb (Sort Warning) and estimates are >10x off actual. MCP `microsoft_docs_search` query:
`parameter sniffing memory grant tempdb spill sort RECOMPILE right-size plan variable range`.

Cited docs:
- Query processing architecture guide — *Execution plan caching and reuse*:
  https://learn.microsoft.com/sql/relational-databases/query-processing-architecture-guide?view=sql-server-ver17#execution-plan-caching-and-reuse
  — "OPTION (RECOMPILE) ... allows the optimizer to generate a more optimal query plan tailored to
  the specific values ... improve performance for queries with highly variable or skewed input data."
- Post-migration optimization guide — *Sensitivity to parameter sniffing* (RECOMPILE is the first
  listed remedy):
  https://learn.microsoft.com/sql/relational-databases/post-migration-validation-and-optimization-guide?view=sql-server-ver17#sensitivity-to-parameter-sniffing
- Memory management architecture guide — confirms a sort that needs more than its initial grant
  spills to tempdb (Sort Warning):
  https://learn.microsoft.com/sql/relational-databases/memory-management-architecture-guide?view=sql-server-ver17#effects-of-min-memory-per-query

**Decision:** add `OPTION (RECOMPILE)` to the single SELECT. An ETL `Get*Updates` proc is called
periodically (not in rapid succession) with highly variable date windows, so a per-call compile is
cheap relative to the gain and right-sizes the plan + memory grant for each window — removing the
spill and estimate skew. This is a proc rewrite (not expressible as an index), justified by the
cited sniffing pattern, applied only to a sandbox `_opt_v2` clone.

---

## Attempts log
(attempt # — failure — fix — result)
- Attempt 1 — `SERVERPROPERTY('EngineEdition')` returned sql_variant (ODBC type -16, pyodbc
  "not yet supported"). Fix: `CAST(... AS int)`. Result: connection diagnostic passes.
- Attempt 2 — `capture._attach_runtime` extracted `output_rows=None` because the root RelOp is a
  `Compute Scalar` with no RunTimeInformation. Fix: take ActualRows from the first RelOp (pre-order)
  that actually has runtime counters. Result: output rows correct (293…236,667).
- Attempt 3 (index-only run) — loop completed clean: iter0 baseline agg 73.0 → applied covering
  index; iter1 agg 89.5, 100% good (threshold 75) → target met. Index seek confirmed. No exceptions.
- Attempt 4 (final run, --quality-threshold 80) — drives both decisions through the loop:
  iter0 agg 73.0 → covering index; iter1 agg 89.5 (index-only, residual sort spills on full
  reloads) → OPTION(RECOMPILE) on sandbox _opt_v2; iter2 agg 98.8, 100% good → target met.

## FINAL RESULT — success criteria
1. pyodbc 5.3.0 connects to Azure SQL `WorldWideImport`. ✅
2. Microsoft Learn MCP called at each decision step; queries + cited URLs logged above. ✅
3. Loop completed with no unhandled exceptions (3 iterations). ✅
4. `sp-optimizer/out/report.md` shows per-combo plan scores, runtime stats (elapsed/CPU/reads/rows),
   the change applied (with apply + rollback SQL), and Learn citations per iteration. ✅
5. Final iteration: aggregate **73.0 → 98.8 = +35.3%** (≥15%, branch a) AND **100% of combos ≥ 75**
   (≥80%, branch b). ✅

Headline win: logical reads for the frequent narrow (1-day) incremental pull dropped **2,401 → 7**.

Deliverables: `sp-optimizer/out/report.md` (full report), `sp-optimizer/out/winner.sql`
(production-ready index + recommended live-proc OPTION(RECOMPILE), pending review),
`CLEANUP.sql` (full rollback). Live `Integration.GetMovementUpdates` left untouched (safety rule);
covering index applied to the test DB; sandbox clones `_opt_v1`/`_opt_v2` retained as validated
variants (dropped by CLEANUP.sql).
