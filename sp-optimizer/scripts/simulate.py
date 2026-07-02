"""Proc-specific load simulation + under-load A/B validation.

Plan shape is decided by statistics and parameters, not concurrency — so the
optimizer loop tunes plans idle. What ONLY load reveals is waits: blocking,
memory-grant queuing, tempdb and log contention, and the write tax of a new
index. This module drives exactly that second phase:

  - The traffic profile is the proc's OWN discovered workload: the weighted
    combos from ``discover`` (Query Store real calls + the data-derived
    sniffing spread) fired at weighted random from N threads. No hand-written
    load script, and the load is proc-shaped by construction.
  - ``--compare-proc`` runs a paired A/B: every worker executes the baseline
    and the candidate back-to-back with the SAME combo, so latency deltas are
    paired samples, not two separately-noisy runs.
  - Each worker snapshots its session wait stats before/after, so the report
    says WHAT the procs waited on under contention, not just how long they took.

EXECUTES the procedure continuously — point it at non-prod only.

Usage (from the sp-optimizer/ directory):
    python -m scripts.simulate --proc "Integration.GetMovementUpdates" \
        --threads 8 --duration 60
    python -m scripts.simulate --proc "dbo.p" --compare-proc "dbo.p_opt_v2" \
        --threads 8 --duration 120 --out ab.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent.parent / ".env")
except ImportError:
    pass

from . import discover
from .capture import _arg_list, _session_wait_snapshot, _wait_delta
from .models import ParamCombo


class ProcStats:
    """Thread-safe latency/error accumulator for one procedure."""

    def __init__(self, name: str):
        self.name = name
        self.lock = threading.Lock()
        self.latencies_ms: list[float] = []
        self.errors = 0
        self.first_errors: list[str] = []

    def record(self, ms: float):
        with self.lock:
            self.latencies_ms.append(ms)

    def record_error(self, msg: str):
        with self.lock:
            self.errors += 1
            if len(self.first_errors) < 3:
                self.first_errors.append(msg)


def percentile(sorted_vals: list[float], pct: float) -> float:
    """Nearest-rank percentile over an already-sorted list."""
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, int(round(pct / 100.0 * len(sorted_vals))) - 1))
    return sorted_vals[k]


def summarize(stats: ProcStats, elapsed_s: float) -> dict:
    with stats.lock:
        lat = sorted(stats.latencies_ms)
        errors = stats.errors
        first_errors = list(stats.first_errors)
    n = len(lat)
    return {
        "proc": stats.name,
        "executions": n,
        "errors": errors,
        "first_errors": first_errors,
        "throughput_per_s": round(n / elapsed_s, 2) if elapsed_s > 0 else 0.0,
        "mean_ms": round(sum(lat) / n, 1) if n else None,
        "p50_ms": round(percentile(lat, 50), 1) if n else None,
        "p95_ms": round(percentile(lat, 95), 1) if n else None,
        "max_ms": round(lat[-1], 1) if n else None,
    }


def _exec_once(cursor, proc_name: str, combo: ParamCombo) -> float:
    """Execute the proc with one combo, draining every result set. Returns ms."""
    args = _arg_list(combo)
    stmt = f"EXEC {proc_name} {args};" if args else f"EXEC {proc_name};"
    t0 = time.perf_counter()
    cursor.execute(stmt)
    while True:
        try:
            cursor.fetchall()
        except Exception:
            pass  # statement returned no rows
        if not cursor.nextset():
            break
    return (time.perf_counter() - t0) * 1000.0


def _worker(
    conn_str: str,
    procs: list[str],
    combos: list[ParamCombo],
    weights: list[float],
    deadline: float,
    stats_by_proc: dict[str, ProcStats],
    wait_sink: list[dict],
    stop: threading.Event,
):
    import pyodbc

    try:
        conn = pyodbc.connect(conn_str, autocommit=True)
    except Exception as e:
        for p in procs:
            stats_by_proc[p].record_error(f"connect failed: {e}")
        return
    cursor = conn.cursor()
    rng = random.Random()
    waits_before = _session_wait_snapshot(cursor)

    while time.time() < deadline and not stop.is_set():
        combo = rng.choices(combos, weights=weights, k=1)[0]
        # Paired A/B: every proc sees the SAME combo, back-to-back.
        for proc in procs:
            try:
                ms = _exec_once(cursor, proc, combo)
                stats_by_proc[proc].record(ms)
            except Exception as e:
                stats_by_proc[proc].record_error(f"{combo.label or 'combo'}: {e}")

    delta = _wait_delta(waits_before, _session_wait_snapshot(cursor), top=10)
    if delta:
        wait_sink.append(delta)
    try:
        conn.close()
    except Exception:
        pass


def merge_waits(per_worker: list[dict], top: int = 8) -> dict:
    total: dict[str, float] = {}
    for waits in per_worker:
        for wt, ms in waits.items():
            total[wt] = total.get(wt, 0.0) + ms
    return dict(sorted(total.items(), key=lambda kv: kv[1], reverse=True)[:top])


def run_simulation(
    conn_str: str,
    proc: str,
    compare_proc: str | None,
    threads: int,
    duration_s: int,
    max_combos: int,
) -> dict:
    import pyodbc

    conn = pyodbc.connect(conn_str, autocommit=True)
    cursor = conn.cursor()
    _, combos = discover.discover(cursor, proc, max_combos=max_combos)
    conn.close()
    if not combos:
        raise RuntimeError(f"no workload combos could be derived for {proc}")

    procs = [proc] + ([compare_proc] if compare_proc else [])
    weights = [c.weight for c in combos]
    stats_by_proc = {p: ProcStats(p) for p in procs}
    wait_sink: list[dict] = []
    stop = threading.Event()
    deadline = time.time() + duration_s

    print(f"simulate · {len(combos)} combo(s), {threads} thread(s), {duration_s}s "
          f"against {' vs '.join(procs)}")
    workers = [
        threading.Thread(
            target=_worker,
            args=(conn_str, procs, combos, weights, deadline, stats_by_proc,
                  wait_sink, stop),
            daemon=True,
        )
        for _ in range(threads)
    ]
    t0 = time.time()
    for w in workers:
        w.start()
    try:
        for w in workers:
            w.join()
    except KeyboardInterrupt:
        stop.set()
        for w in workers:
            w.join(timeout=10)
    elapsed = time.time() - t0

    report: dict = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "duration_s": round(elapsed, 1),
        "threads": threads,
        "combos": [{"label": c.label, "weight": c.weight} for c in combos],
        "results": [summarize(stats_by_proc[p], elapsed) for p in procs],
        "top_waits_ms": merge_waits(wait_sink),
    }
    if compare_proc:
        base, cand = report["results"][0], report["results"][1]
        if base["p50_ms"] and cand["p50_ms"]:
            report["comparison"] = {
                "p50_delta_pct": round((cand["p50_ms"] - base["p50_ms"])
                                       / base["p50_ms"] * 100.0, 1),
                "p95_delta_pct": round((cand["p95_ms"] - base["p95_ms"])
                                       / base["p95_ms"] * 100.0, 1)
                                 if base["p95_ms"] and cand["p95_ms"] else None,
            }
    return report


def _print_report(report: dict) -> None:
    for r in report["results"]:
        print(f"\n{r['proc']}")
        print(f"  executions : {r['executions']}  ({r['throughput_per_s']}/s)"
              f"  errors: {r['errors']}")
        if r["executions"]:
            print(f"  latency ms : mean {r['mean_ms']} · p50 {r['p50_ms']} · "
                  f"p95 {r['p95_ms']} · max {r['max_ms']}")
        for e in r["first_errors"]:
            print(f"  error      : {e}")
    if report.get("top_waits_ms"):
        print("\ntop waits (ms, all sessions):")
        for wt, ms in report["top_waits_ms"].items():
            print(f"  {wt:<30} {ms:,.0f}")
    if report.get("comparison"):
        c = report["comparison"]
        print(f"\nA/B: candidate vs baseline · p50 {c['p50_delta_pct']:+.1f}% · "
              f"p95 {c['p95_delta_pct']:+.1f}%"
              if c.get("p95_delta_pct") is not None
              else f"\nA/B: candidate vs baseline · p50 {c['p50_delta_pct']:+.1f}%")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Proc-shaped load simulator / under-load A/B validator "
                    "(EXECUTES the proc — non-prod only)")
    ap.add_argument("--proc", required=True, help="schema-qualified proc name (baseline)")
    ap.add_argument("--compare-proc", default=None,
                    help="candidate variant (e.g. the winner sandbox) for a "
                         "paired A/B against --proc")
    ap.add_argument("--conn", default=os.environ.get("SQL_CONNECTION_STRING"),
                    help="pyodbc connection string (defaults to SQL_CONNECTION_STRING)")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--duration", type=int, default=60, help="seconds (default 60)")
    ap.add_argument("--max-combos", type=int, default=12)
    ap.add_argument("--out", default=None, help="also write the report as JSON here")
    args = ap.parse_args(argv)

    if not args.conn:
        ap.error("--conn is required (or set SQL_CONNECTION_STRING in .env / environment)")
    try:
        import pyodbc  # noqa: F401
    except ImportError:
        print("pyodbc is required: pip install pyodbc", file=sys.stderr)
        return 2

    report = run_simulation(args.conn, args.proc, args.compare_proc,
                            args.threads, args.duration, args.max_combos)
    _print_report(report)
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nreport written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
