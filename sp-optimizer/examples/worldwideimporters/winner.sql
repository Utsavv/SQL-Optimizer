/* ============================================================================
   winner.sql — recommended production changes for Integration.GetMovementUpdates
   Target: WorldWideImport (Azure SQL Database)

   Result across a 7-combo workload (narrow incremental pulls -> full reloads):
     baseline aggregate plan score 73.0  ->  98.8   (+35.3%)
     logical reads for the frequent narrow (1-day) pull: 2,401 -> 7

   Change 1 (the covering index) is low-risk and already applied to the test DB.
   Change 2 (OPTION (RECOMPILE) on the live proc) is a proc rewrite: REVIEW and
   apply with approval. The optimizer loop validated it only on a sandbox clone;
   the live procedure was deliberately left untouched.
   ============================================================================ */

----------------------------------------------------------------------------
-- Change 1: covering index on the date-range filter column.
--   Converts the per-call full scan of ~236,667 rows into a tight range seek
--   for the common narrow-window incremental-ETL calls, and fully covers the
--   query (no key lookups). Filter column is the key; projected columns are
--   INCLUDE; StockItemTransactionID (clustered key) is auto-included.
--   Docs: https://learn.microsoft.com/sql/relational-databases/sql-server-index-design-guide?view=sql-server-ver17#nonclustered-index-design-guidelines
----------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_StockItemTransactions_LastEditedWhen_Covering'
                 AND object_id = OBJECT_ID('Warehouse.StockItemTransactions'))
BEGIN
    CREATE NONCLUSTERED INDEX IX_StockItemTransactions_LastEditedWhen_Covering
    ON Warehouse.StockItemTransactions (LastEditedWhen)
    INCLUDE (TransactionOccurredWhen, InvoiceID, PurchaseOrderID, Quantity,
             StockItemID, CustomerID, SupplierID, TransactionTypeID);
END;
GO

----------------------------------------------------------------------------
-- Change 2 (REVIEW before applying to production): add OPTION (RECOMPILE) to
--   remove the residual parameter-sniffing tail. After Change 1 the single
--   cached plan is sniffed on whichever window runs first; reusing a plan whose
--   memory grant was sized for a narrow window makes the ORDER BY sort spill to
--   tempdb on full reloads. RECOMPILE gives each periodic ETL call a plan sized
--   for its own window (seek for narrow, ordered clustered scan for full
--   reloads -> no sort spill). Appropriate here because the proc runs
--   periodically, not in rapid succession.
--   Docs: https://learn.microsoft.com/sql/relational-databases/query-processing-architecture-guide?view=sql-server-ver17#execution-plan-caching-and-reuse
----------------------------------------------------------------------------
ALTER PROCEDURE Integration.GetMovementUpdates
@LastCutoff datetime2(7),
@NewCutoff datetime2(7)
WITH EXECUTE AS OWNER
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    SELECT CAST(sit.TransactionOccurredWhen AS date) AS [Date Key],
           sit.StockItemTransactionID AS [WWI Stock Item Transaction ID],
           sit.InvoiceID AS [WWI Invoice ID],
           sit.PurchaseOrderID AS [WWI Purchase Order ID],
           CAST(sit.Quantity AS int) AS Quantity,
           sit.StockItemID AS [WWI Stock Item ID],
           sit.CustomerID AS [WWI Customer ID],
           sit.SupplierID AS [WWI Supplier ID],
           sit.TransactionTypeID AS [WWI Transaction Type ID],
           sit.TransactionOccurredWhen AS [Transaction Occurred When]
    FROM Warehouse.StockItemTransactions AS sit
    WHERE sit.LastEditedWhen > @LastCutoff
    AND sit.LastEditedWhen <= @NewCutoff
    ORDER BY sit.StockItemTransactionID
    OPTION (RECOMPILE);

    RETURN 0;
END;
GO
