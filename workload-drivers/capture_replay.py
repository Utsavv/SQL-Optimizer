#!/usr/bin/env python3
"""Capture real stored-procedure calls with Extended Events, then replay them.

The most realistic load is the load you actually had. This driver pairs with
``xe_capture.sql`` (which records every RPC call of a chosen proc — statement
text + timestamp — into a ring buffer):

  capture   Read the ring buffer of the ``sp_optimizer_capture`` XE session
            and write one JSON line per call: {"ts": ..., "statement": ...}.
            Harvest before the ring buffer wraps; re-run to append new calls
            (duplicates are dropped on replay by (ts, statement) identity).

  replay    Re-issue the captured calls with their ORIGINAL relative timing
            (or accelerated with --speed, or as fast as possible with
            --speed 0) across N worker threads. Reports throughput and
            latency while running.

Replay EXECUTES the captured statements — point it at a NON-PRODUCTION copy
of the database. Distributed Replay was deprecated in SQL Server 2022; this
is the small, proc-focused replacement for this repo's tuning workflow.

Examples:
    # after running xe_capture.sql and letting traffic flow
    python workload-drivers/capture_replay.py capture --out calls.jsonl

    # replay at original pace with 8 workers against a test database
    python workload-drivers/capture_replay.py replay --calls calls.jsonl --threads 8

    # replay 10x faster
    python workload-drivers/capture_replay.py replay --calls calls.jsonl --speed 10
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from common import Stats, connect, resolve_conn_str

SESSION_NAME = "sp_optimizer_capture"

# Database-scoped DMVs (Azure SQL DB) first, then server-scoped (full instance).
_RING_BUFFER_QUERIES = (
    """
    SELECT CAST(t.target_data AS nvarchar(max))
    FROM sys.dm_xe_database_session_targets t
    JOIN sys.dm_xe_database_sessions s ON s.address = t.event_session_address
    WHERE s.name = ? AND t.target_name = N'ring_buffer';
    """,
    """
    SELECT CAST(t.target_data AS nvarchar(max))
    FROM sys.dm_xe_session_targets t
    JOIN sys.dm_xe_sessions s ON s.address = t.event_session_address
    WHERE s.name = ? AND t.target_name = N'ring_buffer';
    """,
)


def read_ring_buffer(cursor, session: str) -> str | None:
    """Return the ring buffer XML for the session, or None if not found."""
    for sql in _RING_BUFFER_QUERIES:
        try:
            cursor.execute(sql, session)
            row = cursor.fetchone()
            if row and row[0]:
                return row[0]
        except Exception:
            continue
    return None


def parse_events(target_xml: str) -> list[dict]:
    """Parse rpc_completed events out of ring-buffer XML.

    Returns [{"ts": iso8601, "statement": str}, ...] in capture order."""
    calls: list[dict] = []
    root = ET.fromstring(target_xml)
    for ev in root.iter("event"):
        if ev.get("name") != "rpc_completed":
            continue
        ts = ev.get("timestamp")  # UTC ISO 8601
        statement = None
        for data in ev.iter("data"):
            if data.get("name") == "statement":
                value = data.find("value")
                statement = value.text if value is not None else None
                break
        if ts and statement:
            calls.append({"ts": ts, "statement": statement.strip()})
    return calls


def cmd_capture(args) -> int:
    conn = connect(resolve_conn_str(args.conn))
    cursor = conn.cursor()
    target_xml = read_ring_buffer(cursor, args.session)
    conn.close()
    if not target_xml:
        print(f"ERROR: XE session '{args.session}' not found or has no ring buffer. "
              f"Run workload-drivers/xe_capture.sql first (edited for your proc).",
              file=sys.stderr)
        return 1

    calls = parse_events(target_xml)
    if args.proc:
        needle = args.proc.lower()
        calls = [c for c in calls if needle in c["statement"].lower()]
    if not calls:
        print("no matching rpc_completed events in the ring buffer (yet)")
        return 1

    out = Path(args.out)
    mode = "a" if out.exists() and args.append else "w"
    with open(out, mode, encoding="utf-8") as f:
        for c in calls:
            f.write(json.dumps(c) + "\n")
    print(f"wrote {len(calls)} call(s) to {out} "
          f"({'appended' if mode == 'a' else 'created'})")
    return 0


# ---- replay -------------------------------------------------------------------

def load_calls(path: str) -> list[dict]:
    """Load, dedupe, and time-sort captured calls from a JSONL file."""
    seen: set[tuple] = set()
    calls: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            c = json.loads(line)
            key = (c["ts"], c["statement"])
            if key in seen:
                continue
            seen.add(key)
            calls.append(c)
    calls.sort(key=lambda c: c["ts"])
    return calls


def _parse_ts(ts: str) -> float:
    """XE timestamps are UTC ISO 8601 (e.g. 2026-07-02T08:15:12.345Z)."""
    ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts).astimezone(timezone.utc).timestamp()


def schedule_offsets(calls: list[dict], speed: float) -> list[float]:
    """Per-call start offsets (seconds from replay start).

    speed=1 keeps the original pacing, speed=10 compresses it 10x, and
    speed=0 means no pacing at all (fire as fast as workers allow)."""
    if not calls:
        return []
    if speed <= 0:
        return [0.0] * len(calls)
    t0 = _parse_ts(calls[0]["ts"])
    return [(_parse_ts(c["ts"]) - t0) / speed for c in calls]


def _replay_worker(conn_str: str, calls: list[dict], offsets: list[float],
                   next_idx: list[int], idx_lock: threading.Lock,
                   start_at: float, stats: Stats, stop: threading.Event):
    conn = connect(conn_str)
    cursor = conn.cursor()
    while not stop.is_set():
        with idx_lock:
            i = next_idx[0]
            if i >= len(calls):
                break
            next_idx[0] += 1
        wait = start_at + offsets[i] - time.time()
        if wait > 0:
            if stop.wait(wait):
                break
        t0 = time.perf_counter()
        try:
            cursor.execute(calls[i]["statement"])
            while True:
                try:
                    cursor.fetchall()
                except Exception:
                    pass
                if not cursor.nextset():
                    break
            stats.record((time.perf_counter() - t0) * 1000.0)
        except Exception as e:
            stats.record_error()
            if stats.errors <= 3:
                print(f"replay error: {e}", file=sys.stderr)
    conn.close()


def cmd_replay(args) -> int:
    conn_str = resolve_conn_str(args.conn)
    calls = load_calls(args.calls)
    if not calls:
        print(f"no calls in {args.calls}", file=sys.stderr)
        return 1
    offsets = schedule_offsets(calls, args.speed)
    span = offsets[-1] if offsets else 0.0
    pacing = "as fast as possible" if args.speed <= 0 else f"{args.speed}x speed"
    print(f"replaying {len(calls)} call(s) over ~{span:,.0f}s ({pacing}) "
          f"with {args.threads} thread(s)")

    stats = Stats()
    stop = threading.Event()
    next_idx = [0]
    idx_lock = threading.Lock()
    start_at = time.time() + 1.0  # small lead-in so all workers are ready
    workers = [
        threading.Thread(
            target=_replay_worker,
            args=(conn_str, calls, offsets, next_idx, idx_lock, start_at, stats, stop),
            daemon=True,
        )
        for _ in range(args.threads)
    ]
    for w in workers:
        w.start()
    try:
        while any(w.is_alive() for w in workers):
            time.sleep(args.report_interval)
            ops, avg, errors, elapsed = stats.snapshot()
            print(f"  {ops}/{len(calls)} replayed · {ops / elapsed:,.1f}/s · "
                  f"avg {avg:,.1f} ms · errors {errors}")
    except KeyboardInterrupt:
        stop.set()
        for w in workers:
            w.join(timeout=10)
    ops, avg, errors, elapsed = stats.snapshot()
    print(f"done: {ops} call(s) in {elapsed:,.1f}s · {ops / elapsed:,.1f}/s · "
          f"avg {avg:,.1f} ms · errors {errors}")
    return 0 if errors == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)

    cap = sub.add_parser("capture", help="harvest captured calls from the XE ring buffer")
    cap.add_argument("--conn", default=None)
    cap.add_argument("--session", default=SESSION_NAME)
    cap.add_argument("--proc", default=None,
                     help="extra client-side filter: keep only statements containing this")
    cap.add_argument("--out", default="calls.jsonl")
    cap.add_argument("--append", action="store_true",
                     help="append to --out instead of overwriting")

    rep = sub.add_parser("replay", help="re-issue captured calls (NON-PROD only)")
    rep.add_argument("--conn", default=None)
    rep.add_argument("--calls", default="calls.jsonl")
    rep.add_argument("--threads", type=int, default=4)
    rep.add_argument("--speed", type=float, default=1.0,
                     help="time compression: 1 = original pacing, 10 = 10x faster, "
                          "0 = no pacing (default 1)")
    rep.add_argument("--report-interval", type=int, default=5)

    args = ap.parse_args(argv)
    return cmd_capture(args) if args.command == "capture" else cmd_replay(args)


if __name__ == "__main__":
    raise SystemExit(main())
