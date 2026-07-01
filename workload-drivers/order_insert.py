"""Order-insertion workload driver for WideWorldImporters.

A cross-platform, headless Python port of Microsoft's
`workload-drivers/order-insert` sample (originally a Windows Forms C# app):
https://github.com/microsoft/sql-server-samples/tree/master/samples/databases/wide-world-importers/workload-drivers/order-insert

It spins up N worker threads that concurrently hammer the
`Website.InsertCustomerOrders` stored procedure, generating an intensive OLTP
order-entry workload. Use it to put a heavy, realistic load on the database
while you assess a procedure's performance (e.g. alongside the sp-optimizer
skill in this repo, or while watching Query Store / wait stats).

Each iteration of a worker faithfully reproduces the original driver:
  1. pick a random employee to be the salesperson,
  2. build one order header from a random customer,
  3. build ~7 order lines from random (non-chiller) stock items, occasionally
     adding a chiller item (~3% of the time),
  4. call `Website.InsertCustomerOrders` with the two table-valued parameters
     (`Website.OrderList`, `Website.OrderLineList`) plus the salesperson ids.

Runtime stats (throughput + average insert latency) are printed live.

Example:
    python workload-drivers/order_insert.py --threads 16 --duration 60
    python workload-drivers/order_insert.py --threads 32 --duration 300 \
        --conn "DRIVER={ODBC Driver 18 for SQL Server};SERVER=...;DATABASE=WideWorldImporters;..."

Requires pyodbc >= 4.0.24 (table-valued parameter support) and ODBC Driver
17/18 for SQL Server. Not for production use.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time

from common import Stats, connect, redact_conn, resolve_conn_str

# --- SQL the original C# driver issues, verbatim in intent -------------------

# A random employee to act as the salesperson / order creator.
SQL_SALESPERSON = (
    "SELECT TOP(1) PersonID FROM [Application].People "
    "WHERE IsEmployee <> 0 ORDER BY NEWID();"
)

# One order header row shaped exactly like the Website.OrderList TVP columns:
# (OrderReference, CustomerID, ContactPersonID, ExpectedDeliveryDate,
#  CustomerPurchaseOrderNumber, IsUndersupplyBackordered, Comments,
#  DeliveryInstructions)
SQL_ORDER = (
    "SELECT TOP(1) 1 AS OrderReference, c.CustomerID, "
    "c.PrimaryContactPersonID AS ContactPersonID, "
    "CAST(DATEADD(day, 1, SYSDATETIME()) AS date) AS ExpectedDeliveryDate, "
    "CAST(FLOOR(RAND() * 10000) + 1 AS nvarchar(20)) AS CustomerPurchaseOrderNumber, "
    "CAST(0 AS bit) AS IsUndersupplyBackordered, N'Auto-generated' AS Comments, "
    "c.DeliveryAddressLine1 + N', ' + c.DeliveryAddressLine2 AS DeliveryInstructions "
    "FROM Sales.Customers AS c ORDER BY NEWID();"
)

# 7 order lines from non-chiller stock, shaped like Website.OrderLineList:
# (OrderReference, StockItemID, Description, Quantity)
SQL_ORDER_LINES = (
    "SELECT * FROM (SELECT TOP(7) 1 AS OrderReference, si.StockItemID, "
    "si.StockItemName AS [Description], FLOOR(RAND() * 10) + 1 AS Quantity "
    "FROM Warehouse.StockItems AS si WHERE IsChillerStock = 0 ORDER BY NEWID()) x"
)

# Occasionally append one chiller item (matches the original's rnd < 4 of 100).
SQL_CHILLER_LINE = (
    " UNION ALL SELECT * FROM (SELECT TOP(1) 1 AS OrderReference, si.StockItemID, "
    "si.StockItemName AS [Description], FLOOR(RAND() * 10) + 1 AS Quantity "
    "FROM Warehouse.StockItems AS si WHERE IsChillerStock <> 0 ORDER BY NEWID()) x"
)

# {CALL ...} lets the ODBC driver bind the table-valued parameters as READONLY.
SQL_INSERT = "{CALL Website.InsertCustomerOrders(?, ?, ?, ?)}"


def _do_one_order(cursor, rng) -> None:
    """Build and insert a single customer order, exactly as the C# driver does."""
    cursor.execute(SQL_SALESPERSON)
    salesperson_id = cursor.fetchone()[0]

    cursor.execute(SQL_ORDER)
    order_rows = [tuple(r) for r in cursor.fetchall()]

    lines_sql = SQL_ORDER_LINES
    if rng.randint(1, 99) < 4:
        lines_sql += SQL_CHILLER_LINE
    lines_sql += ";"
    cursor.execute(lines_sql)
    order_line_rows = [tuple(r) for r in cursor.fetchall()]

    # Two TVPs (lists of tuples) + two scalar ids. pyodbc treats a list-of-rows
    # parameter as a table-valued parameter.
    cursor.execute(
        SQL_INSERT, order_rows, order_line_rows, salesperson_id, salesperson_id
    )


def worker(
    task_id: int,
    conn_str: str,
    stop: threading.Event,
    stats: Stats,
    stop_on_error: bool,
    error_sink: list,
) -> None:
    """Loop inserting orders until the stop event is set. Reconnects on
    transient errors so one blip doesn't silently drop a load-generating
    thread (unless --stop-on-error is passed)."""
    import random

    rng = random.Random(task_id)
    while not stop.is_set():
        try:
            with connect(conn_str) as con:
                cur = con.cursor()
                while not stop.is_set():
                    start = time.monotonic()
                    _do_one_order(cur, rng)
                    con.commit()
                    stats.record((time.monotonic() - start) * 1000.0)
        except Exception as ex:  # noqa: BLE001 - report and (optionally) retry
            stats.record_error()
            if len(error_sink) < 5:
                error_sink.append(f"[thread {task_id}] {ex}")
            if stop_on_error:
                stop.set()
                return
            # brief backoff before reconnecting so we don't spin on a hard error
            stop.wait(0.5)


def run(args: argparse.Namespace) -> int:
    conn_str = resolve_conn_str(args.conn)
    print(f"Order-insert workload -> {redact_conn(conn_str)}")
    print(
        f"threads={args.threads}  duration={args.duration}s  "
        f"report-interval={args.report_interval}s"
    )
    print("Target proc: Website.InsertCustomerOrders (WideWorldImporters)\n")

    stop = threading.Event()
    stats = Stats()
    errors: list[str] = []
    threads = [
        threading.Thread(
            target=worker,
            args=(i, conn_str, stop, stats, args.stop_on_error, errors),
            daemon=True,
            name=f"order-{i}",
        )
        for i in range(args.threads)
    ]
    for t in threads:
        t.start()

    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    last_ops = 0
    try:
        while any(t.is_alive() for t in threads):
            stop.wait(args.report_interval)
            ops, avg, errs, elapsed = stats.snapshot()
            interval_ops = ops - last_ops
            last_ops = ops
            rate = interval_ops / args.report_interval
            print(
                f"[{elapsed:7.1f}s] orders={ops:<8} "
                f"rate={rate:8.1f}/s  avg_latency={avg:7.1f}ms  errors={errs}"
            )
            if deadline and time.monotonic() >= deadline:
                break
            if stop.is_set():  # a worker asked to stop (stop-on-error)
                break
    except KeyboardInterrupt:
        print("\nInterrupted - stopping workers...")
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=30)

    ops, avg, errs, elapsed = stats.snapshot()
    print("\n--- summary ---")
    print(f"total orders inserted : {ops}")
    print(f"elapsed               : {elapsed:.1f}s")
    print(f"overall throughput    : {ops / elapsed:.1f} orders/sec")
    print(f"avg insert latency    : {avg:.1f} ms")
    print(f"errors                : {errs}")
    if errors:
        print("\nfirst errors seen:")
        for e in errors:
            print(f"  {e}")
    return 1 if (errs and ops == 0) else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Concurrent order-insert workload driver for WideWorldImporters "
        "(hammers Website.InsertCustomerOrders).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--threads", type=int, default=8, help="number of concurrent worker threads"
    )
    p.add_argument(
        "--duration",
        type=int,
        default=60,
        help="how long to run, in seconds (0 = run until Ctrl+C)",
    )
    p.add_argument(
        "--report-interval",
        type=float,
        default=5.0,
        help="seconds between live throughput reports",
    )
    p.add_argument(
        "--conn",
        default=None,
        help="pyodbc connection string (defaults to SQL_CONNECTION_STRING from .env)",
    )
    p.add_argument(
        "--stop-on-error",
        action="store_true",
        help="stop the whole run on the first error (default: log and reconnect)",
    )
    return p


def main() -> None:
    sys.exit(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
