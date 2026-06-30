/* ============================================================================
   CLEANUP.sql — rollback for every object created during the sp-optimizer run
   Target: WorldWideImport (Azure SQL Database)
   Run against the same DB to return it to its pre-run state.
   ============================================================================ */

-- 1) Covering index created by iteration 1 (the optimization itself).
DROP INDEX IF EXISTS IX_StockItemTransactions_LastEditedWhen_Covering
    ON Warehouse.StockItemTransactions;

-- 2) Sandbox proc clones created by the loop (make_sandbox -> <proc>_opt_v<n>).
--    Drop v1..v5 defensively; only the ones that were created will exist.
DROP PROCEDURE IF EXISTS Integration.GetMovementUpdates_opt_v1;
DROP PROCEDURE IF EXISTS Integration.GetMovementUpdates_opt_v2;
DROP PROCEDURE IF EXISTS Integration.GetMovementUpdates_opt_v3;
DROP PROCEDURE IF EXISTS Integration.GetMovementUpdates_opt_v4;
DROP PROCEDURE IF EXISTS Integration.GetMovementUpdates_opt_v5;

-- Verify nothing is left behind:
--   SELECT name FROM sys.indexes WHERE name = 'IX_StockItemTransactions_LastEditedWhen_Covering';
--   SELECT s.name+'.'+p.name FROM sys.procedures p JOIN sys.schemas s ON p.schema_id=s.schema_id
--     WHERE p.name LIKE 'GetMovementUpdates[_]opt[_]v%';
