# Workload drivers

Load generators that put a **heavy, concurrent OLTP workload** on the
[WideWorldImporters](https://learn.microsoft.com/sql/samples/wide-world-importers-what-is)
sample database — so you can assess how a stored procedure (or the whole server)
behaves *under pressure*, not just when it's idle. Point the
[`sp-optimizer`](../sp-optimizer) skill, Query Store, or your own monitoring at
a procedure while one of these drivers is running to see performance under real
contention.

These are cross-platform, headless **Python ports** of Microsoft's
[`wide-world-importers/workload-drivers`](https://github.com/microsoft/sql-server-samples/tree/master/samples/databases/wide-world-importers/workload-drivers)
sample. The originals are Windows-only C# WinForms apps that need Visual Studio;
these reproduce the same workloads faithfully but run anywhere the rest of this
repo runs (Linux/macOS/Windows), reuse the repo's `SQL_CONNECTION_STRING` /
`.env` convention, and are driven from the command line so they fit into scripts
and CI.

> Not intended for production use — they generate load and write data into the
> sample database.

## Drivers

| Script | Ported from | What it does | Target object |
|---|---|---|---|
| `order_insert.py` | `order-insert/` | N threads continuously insert customer orders, generating an intensive order-entry OLTP load | `Website.InsertCustomerOrders` |
| `vehicle_location.py` | `vehicle-location-insert/` | N threads insert vehicle-location rows, comparing disk-based vs memory-optimized (natively compiled) inserts | `OnDisk.InsertVehicleLocation` / `InMemory.InsertVehicleLocation` |
| `VehicleLocation.sql` | (vendored verbatim) | Creates the `OnDisk` / `InMemory` schemas, tables, and procedures the vehicle driver needs | — |
| `common.py` | — | Shared helpers: connection-string resolution, pyodbc connect, thread-safe running stats | — |

## Prerequisites

1. Python deps (already in the repo root):
   ```bash
   pip install -r requirements.txt
   ```
   Requires **pyodbc ≥ 4.0.24** (table-valued parameter support) and **ODBC
   Driver 17/18 for SQL Server**.
2. A **WideWorldImporters** database to point at. If you don't have one, the
   [`setup/`](../setup) scripts in this repo can stand one up on Azure SQL.
3. A connection string, via either:
   - `SQL_CONNECTION_STRING` in your `.env` (copy `.env.example` → `.env`), or
   - `--conn "..."` on any driver.

## Order-insert workload

Generates a sustained order-entry load by concurrently calling
`Website.InsertCustomerOrders`. Each iteration picks a random salesperson, a
random customer order header, and ~7 random stock-item order lines (occasionally
a chiller item), then inserts them through the two table-valued parameters
`Website.OrderList` and `Website.OrderLineList` — exactly like the original
driver. Live throughput and average insert latency are printed while it runs.

```bash
# 16 threads for 60 seconds, using SQL_CONNECTION_STRING from .env
python workload-drivers/order_insert.py --threads 16 --duration 60

# run until Ctrl+C, explicit connection string
python workload-drivers/order_insert.py --threads 32 --duration 0 \
  --conn "DRIVER={ODBC Driver 18 for SQL Server};SERVER=myserver.database.windows.net;DATABASE=WideWorldImporters;UID=me;PWD=***;Encrypt=yes;"
```

Options: `--threads` (default 8), `--duration` seconds (default 60; `0` = until
Ctrl+C), `--report-interval` seconds (default 5), `--conn`, `--stop-on-error`.

### Assessing SP performance under this load

Start the driver, then in another session capture plans / waits / Query Store
for the procedure you care about while it's contended. For example, drive the
optimizer against a proc while orders are streaming in:

```bash
# terminal 1 — generate load
python workload-drivers/order_insert.py --threads 24 --duration 600

# terminal 2 — assess a proc under that load (see ../sp-optimizer)
cd sp-optimizer && python -m scripts.optimize --proc "Website.InsertCustomerOrders" ...
```

## Vehicle-location workload

Compares insert throughput of a disk-based table vs a memory-optimized,
natively compiled equivalent.

**First**, create the objects once (they are *not* created by the driver):

```bash
# any client works; e.g. sqlcmd or Azure Data Studio
sqlcmd -S <server> -d WideWorldImporters -i workload-drivers/VehicleLocation.sql
```

`VehicleLocation.sql` enables the database for In-Memory OLTP (adding a
`MEMORY_OPTIMIZED_DATA` filegroup on non-Azure engines), then creates
`OnDisk.VehicleLocations` / `InMemory.VehicleLocations` and their insert procs.
In-Memory OLTP requires a supported edition/tier and a user database.

**Then** run the comparison:

```bash
python workload-drivers/vehicle_location.py --threads 8 --rows-per-thread 50000 --mode both
```

Each thread inserts `--rows-per-thread` rows inside a single transaction. With
`--mode both` it runs on-disk then in-memory and prints the speedup.

Options: `--threads` (default 4), `--rows-per-thread` (default 50000), `--mode`
`{ondisk,inmemory,both}` (default `both`), `--conn`.

> If on-disk and in-memory times come out similar, you may be hitting a **log-IO
> bottleneck** (every commit flushes to disk). The original sample suggests
> testing with delayed durability:
> ```sql
> ALTER DATABASE WideWorldImporters SET DELAYED_DURABILITY = FORCED;
> ```
> Only do this on a non-production database — it trades a small durability
> window for throughput.

## Attribution

Ported from the Microsoft SQL Server Samples repository
([`microsoft/sql-server-samples`](https://github.com/microsoft/sql-server-samples),
MIT License), `samples/databases/wide-world-importers/workload-drivers`.
Original authors: Greg Low, Jos de Bruijn. `VehicleLocation.sql` is included
verbatim from that sample.
