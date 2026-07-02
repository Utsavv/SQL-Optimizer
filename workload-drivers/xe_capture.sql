/*
  Extended Events capture session for capture_replay.py — records every real
  call of a stored procedure (statement text + timestamp) into a ring buffer,
  so the replay driver can re-issue the exact production workload.

  TEMPLATE: replace GetMovementUpdates in the WHERE clause below with (part of)
  the name of the procedure you want to capture. The filter matches the RPC
  statement text, so a distinctive fragment of the proc name is enough.

  This DATABASE-scoped variant works on Azure SQL Database and on-prem alike.
  On a full SQL Server instance you may use ON SERVER instead (and the
  server-scoped DMVs) — see the comment at the bottom.

  Overhead is low (ring buffer, ALLOW_SINGLE_EVENT_LOSS), but as with any
  tracing: verify on non-prod first.
*/

-- 1) Create + start the session --------------------------------------------
IF EXISTS (SELECT 1 FROM sys.database_event_sessions WHERE name = N'sp_optimizer_capture')
    DROP EVENT SESSION [sp_optimizer_capture] ON DATABASE;
GO

CREATE EVENT SESSION [sp_optimizer_capture] ON DATABASE
ADD EVENT sqlserver.rpc_completed(
    ACTION (sqlserver.username)
    WHERE sqlserver.like_i_sql_unicode_string([statement], N'%GetMovementUpdates%')
)
ADD TARGET package0.ring_buffer(SET max_memory = 51200)   -- KB (~50 MB)
WITH (EVENT_RETENTION_MODE = ALLOW_SINGLE_EVENT_LOSS,
      MAX_DISPATCH_LATENCY = 5 SECONDS);
GO

ALTER EVENT SESSION [sp_optimizer_capture] ON DATABASE STATE = START;
GO

-- 2) Let real traffic run for a representative window (an hour, a day...),
--    then harvest the events:
--        python workload-drivers/capture_replay.py capture --out calls.jsonl
--    The ring buffer holds a bounded window; harvest before it wraps.

-- 3) Stop + drop the session when done --------------------------------------
-- ALTER EVENT SESSION [sp_optimizer_capture] ON DATABASE STATE = STOP;
-- DROP EVENT SESSION [sp_optimizer_capture] ON DATABASE;

/*
  Full SQL Server instance (server-scoped) variant:

    CREATE EVENT SESSION [sp_optimizer_capture] ON SERVER
    ADD EVENT sqlserver.rpc_completed(
        ACTION (sqlserver.username, sqlserver.database_name)
        WHERE sqlserver.like_i_sql_unicode_string([statement], N'%GetMovementUpdates%')
    )
    ADD TARGET package0.ring_buffer(SET max_memory = 51200)
    WITH (EVENT_RETENTION_MODE = ALLOW_SINGLE_EVENT_LOSS,
          MAX_DISPATCH_LATENCY = 5 SECONDS);
    ALTER EVENT SESSION [sp_optimizer_capture] ON SERVER STATE = START;

  capture_replay.py tries the database-scoped DMVs first and falls back to the
  server-scoped ones automatically.
*/
