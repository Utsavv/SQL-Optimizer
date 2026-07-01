"""Vehicle-location insertion workload driver for WideWorldImporters.

A cross-platform, headless Python port of Microsoft's
`workload-drivers/vehicle-location-insert` sample (originally a Windows Forms
C# app):
https://github.com/microsoft/sql-server-samples/tree/master/samples/databases/wide-world-importers/workload-drivers/vehicle-location-insert

It compares the performance of inserting rows into a traditional disk-based
table (`OnDisk.InsertVehicleLocation`) versus a memory-optimized / natively
compiled equivalent (`InMemory.InsertVehicleLocation`). Each worker thread opens
its own connection, runs one transaction, and inserts `--rows-per-thread` rows
inside it, matching the original driver.

PREREQUISITE: run VehicleLocation.sql (in this folder) against the target
database once first, to create the OnDisk / InMemory schemas, tables, and
procedures. This driver does not create them.

Example:
    # create the objects once
    sqlcmd -S <server> -d WideWorldImporters -i workload-drivers/VehicleLocation.sql
    # then compare
    python workload-drivers/vehicle_location.py --threads 8 --rows-per-thread 50000 --mode both

If on-disk and in-memory times are similar, you may be hitting a log-IO
bottleneck; see the note in README.md about DELAYED_DURABILITY. Not for
production use.
"""
from __future__ import annotations

import argparse
import random
import sys
import threading
import time

from common import connect, redact_conn, resolve_conn_str

PROC = {
    "ondisk": "OnDisk.InsertVehicleLocation",
    "inmemory": "InMemory.InsertVehicleLocation",
}


def worker(
    proc: str,
    conn_str: str,
    rows_per_thread: int,
    errors: list,
) -> None:
    """Insert rows_per_thread rows in a single transaction via the given proc."""
    sql = f"{{CALL {proc}(?, ?, ?, ?)}}"
    try:
        with connect(conn_str) as con:
            # One explicit transaction per thread, committed at the end - the
            # same shape as the original driver (BeginTransaction / Commit).
            con.autocommit = False
            cur = con.cursor()
            rng = random.Random()
            for _ in range(rows_per_thread):
                cur.execute(
                    sql,
                    "EA24-GL",              # RegistrationNumber
                    time.strftime("%Y-%m-%d %H:%M:%S"),  # TrackedWhen
                    rng.randint(0, 99),     # Longitude
                    rng.randint(0, 99),     # Latitude
                )
            con.commit()
    except Exception as ex:  # noqa: BLE001
        errors.append(str(ex))


def run_mode(mode: str, conn_str: str, threads: int, rows_per_thread: int) -> float:
    """Run one insertion mode across `threads` threads; return elapsed seconds."""
    proc = PROC[mode]
    errors: list[str] = []
    workers = [
        threading.Thread(
            target=worker,
            args=(proc, conn_str, rows_per_thread, errors),
            name=f"{mode}-{i}",
        )
        for i in range(threads)
    ]
    start = time.monotonic()
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    elapsed = time.monotonic() - start

    total_rows = threads * rows_per_thread
    print(
        f"{mode:>8}: {total_rows} rows via {proc} in {elapsed * 1000:.0f} ms "
        f"({total_rows / max(elapsed, 1e-9):,.0f} rows/sec)"
    )
    if errors:
        print(f"          {len(errors)} thread error(s); first: {errors[0]}")
    return elapsed


def run(args: argparse.Namespace) -> int:
    conn_str = resolve_conn_str(args.conn)
    print(f"Vehicle-location workload -> {redact_conn(conn_str)}")
    print(
        f"threads={args.threads}  rows-per-thread={args.rows_per_thread}  "
        f"mode={args.mode}\n"
    )

    modes = ["ondisk", "inmemory"] if args.mode == "both" else [args.mode]
    results = {}
    for mode in modes:
        results[mode] = run_mode(
            mode, conn_str, args.threads, args.rows_per_thread
        )

    if len(results) == 2 and all(v > 0 for v in results.values()):
        on, mem = results["ondisk"], results["inmemory"]
        if mem > 0:
            print(f"\nin-memory speedup: {on / mem:.2f}x vs on-disk")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Vehicle-location insert workload driver comparing on-disk vs "
        "in-memory tables in WideWorldImporters. Run VehicleLocation.sql first.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--threads", type=int, default=4, help="concurrent worker threads")
    p.add_argument(
        "--rows-per-thread",
        type=int,
        default=50000,
        help="rows each thread inserts inside one transaction",
    )
    p.add_argument(
        "--mode",
        choices=["ondisk", "inmemory", "both"],
        default="both",
        help="which table(s) to insert into",
    )
    p.add_argument(
        "--conn",
        default=None,
        help="pyodbc connection string (defaults to SQL_CONNECTION_STRING from .env)",
    )
    return p


def main() -> None:
    sys.exit(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
