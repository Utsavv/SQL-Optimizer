"""Step 4–6 orchestration: the closed optimization loop + CLI.

Wires discover -> capture -> analyze -> decide -> apply -> verify and repeats
until a termination condition is met. Everything is applied to a SANDBOX copy
of the procedure; the live object is never modified.
"""
from __future__ import annotations

import argparse
import sys

from . import analyze, capture, discover
from .llm import ClaudeBackend, GeminiBackend, LLMBackend
from .models import (
    Change,
    IterationResult,
    fraction_good,
    workload_score,
)


# ---- sandbox management -----------------------------------------------------

def get_proc_text(cursor, proc_name: str) -> str:
    cursor.execute("SELECT OBJECT_DEFINITION(OBJECT_ID(?));", proc_name)
    row = cursor.fetchone()
    return row[0] if row and row[0] else ""


def make_sandbox(cursor, proc_name: str, version: int) -> str:
    """Clone the proc into <proc>_opt_v<n> so the live object is never touched."""
    original = get_proc_text(cursor, proc_name)
    if not original:
        raise RuntimeError(f"Could not read definition of {proc_name}")
    sandbox_name = f"{proc_name}_opt_v{version}"
    short = sandbox_name.split(".")[-1].strip("[]")
    body = original
    # naive rename of CREATE/ALTER target; real impl should parse robustly
    for kw in ("CREATE PROCEDURE", "CREATE PROC", "ALTER PROCEDURE", "ALTER PROC"):
        if kw in body.upper():
            idx = body.upper().find(kw)
            head = body[: idx + len(kw)]
            tail = body[idx + len(kw):]
            # replace the first token (proc name) in tail
            tail_stripped = tail.lstrip()
            name_end = len(tail) - len(tail_stripped)
            after_name = tail_stripped.split(None, 1)
            rest = after_name[1] if len(after_name) > 1 else ""
            body = f"{head} {short} {rest}"
            break
    cursor.execute(f"IF OBJECT_ID('{sandbox_name}') IS NOT NULL DROP PROCEDURE {sandbox_name};")
    cursor.execute(body)
    return sandbox_name


# ---- the loop ---------------------------------------------------------------

def run_loop(
    cursor,
    proc_name: str,
    backend: LLMBackend,
    max_iterations: int = 5,
    target_fraction: float = 0.8,
    quality_threshold: float = 75.0,
    regression_tolerance: float = 10.0,
    use_actual: bool = False,
    max_combos: int = 12,
) -> list[IterationResult]:
    params, combos = discover.discover(cursor, proc_name, max_combos=max_combos)
    history: list[IterationResult] = []
    current_proc = proc_name
    prev_aggregate = -1.0

    for it in range(max_iterations):
        caps = capture.capture_workload(cursor, current_proc, combos, actual=use_actual)
        scores = analyze.analyze_workload(caps)
        agg = workload_score(scores, combos)
        frac = fraction_good(scores, quality_threshold)

        result = IterationResult(
            iteration=it, scores=scores, aggregate_score=agg, fraction_good=frac
        )

        # termination: good enough
        if frac >= target_fraction:
            history.append(result)
            print(f"[iter {it}] target met: {frac:.0%} good, agg={agg:.1f}")
            break

        # termination: stalled
        if it > 0 and agg <= prev_aggregate + 0.5:
            history.append(result)
            print(f"[iter {it}] stalled (agg={agg:.1f} vs prev {prev_aggregate:.1f})")
            break

        # decision step
        proc_text = get_proc_text(cursor, current_proc)
        change = backend.propose_change(proc_text, scores)
        if change.kind == "none" or not change.apply_sql.strip():
            history.append(result)
            print(f"[iter {it}] no safe change proposed; stopping")
            break

        # apply to a fresh sandbox, then verify next iteration
        sandbox = make_sandbox(cursor, proc_name, it + 1)
        try:
            for stmt in _split_batches(change.apply_sql):
                if stmt.strip():
                    cursor.execute(stmt)
        except Exception as e:
            result.regressions.append(f"apply failed: {e}")
            history.append(result)
            print(f"[iter {it}] apply failed: {e}")
            break

        result.change_applied = change
        history.append(result)
        current_proc = sandbox
        prev_aggregate = agg
        print(f"[iter {it}] applied {change.kind}: {change.target_object} (agg={agg:.1f})")

    return history


def _split_batches(sql: str) -> list[str]:
    """Split on GO batch separators."""
    out, buf = [], []
    for line in sql.splitlines():
        if line.strip().upper() == "GO":
            out.append("\n".join(buf))
            buf = []
        else:
            buf.append(line)
    if buf:
        out.append("\n".join(buf))
    return out


# ---- CLI --------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="AI-driven SQL Server SP optimizer")
    ap.add_argument("--proc", required=True, help="schema-qualified proc name")
    ap.add_argument("--conn", required=True, help="pyodbc connection string")
    ap.add_argument("--backend", choices=["gemini", "claude"], default="gemini")
    ap.add_argument("--model", default=None)
    ap.add_argument("--project", default=None, help="GCP project (gemini backend)")
    ap.add_argument("--max-iterations", type=int, default=5)
    ap.add_argument("--target-fraction", type=float, default=0.8)
    ap.add_argument("--quality-threshold", type=float, default=75.0)
    ap.add_argument("--max-combos", type=int, default=12)
    ap.add_argument("--actual", action="store_true",
                    help="run ACTUAL plans (executes proc — non-prod only)")
    ap.add_argument("--report", default="report.md")
    args = ap.parse_args(argv)

    try:
        import pyodbc
    except ImportError:
        print("pyodbc is required: pip install pyodbc", file=sys.stderr)
        return 2

    if args.backend == "gemini":
        backend = GeminiBackend(model=args.model or "gemini-1.5-flash", project=args.project)
    else:
        backend = ClaudeBackend(model=args.model or "claude-sonnet-4-6")

    conn = pyodbc.connect(args.conn, autocommit=True)
    cursor = conn.cursor()

    history = run_loop(
        cursor,
        args.proc,
        backend,
        max_iterations=args.max_iterations,
        target_fraction=args.target_fraction,
        quality_threshold=args.quality_threshold,
        use_actual=args.actual,
        max_combos=args.max_combos,
    )

    write_report(history, args.report)
    print(f"\nDone. {len(history)} iterations. Report: {args.report}")
    return 0


def write_report(history: list[IterationResult], path: str):
    lines = ["# SP Optimization Report\n"]
    for r in history:
        lines.append(f"## Iteration {r.iteration}")
        lines.append(f"- Aggregate workload score: **{r.aggregate_score:.1f}**")
        lines.append(f"- Fraction good: **{r.fraction_good:.0%}**")
        if r.change_applied:
            c = r.change_applied
            lines.append(f"- Change applied: **{c.kind}** — {c.target_object}")
            lines.append(f"  - Rationale: {c.rationale}")
        if r.regressions:
            lines.append(f"- Regressions: {', '.join(r.regressions)}")
        lines.append("\n### Per-combo scores")
        for s in r.scores:
            warn = f" — {'; '.join(s.warnings)}" if s.warnings else ""
            lines.append(f"  - `{s.combo_label}`: {s.score:.1f}{warn}")
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())
