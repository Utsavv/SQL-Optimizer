"""Step 4–6 orchestration: the closed optimization loop + CLI.

Wires discover -> capture -> analyze -> decide -> apply -> verify and repeats
until a termination condition is met. Everything is applied to a SANDBOX copy
of the procedure; the live object is never modified.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Load .env from the project root (two levels up from this file).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent.parent / ".env")
except ImportError:
    pass  # python-dotenv not installed; rely on environment variables directly

from . import analyze, capture, discover
from .llm import ClaudeBackend, FileBackend, GeminiBackend, LLMBackend
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


# CREATE/ALTER PROC[EDURE] followed by a schema-qualified, optionally bracketed,
# proc name:  CREATE PROCEDURE [Integration].[GetMovementUpdates]
_PROC_HEADER_RE = re.compile(
    r"(?P<kw>CREATE\s+PROC(?:EDURE)?|ALTER\s+PROC(?:EDURE)?)\s+"
    r"(?P<name>(?:\[[^\]]+\]|[A-Za-z0-9_@#$]+)\s*(?:\.\s*(?:\[[^\]]+\]|[A-Za-z0-9_@#$]+))?)",
    re.IGNORECASE,
)


def _split_schema_proc(proc_name: str) -> tuple[str, str]:
    """Split 'Integration.GetMovementUpdates' (or bracketed forms) into
    (schema, name) with brackets stripped."""
    parts = re.findall(r"\[[^\]]+\]|[^.\s]+", proc_name)
    parts = [p.strip("[]") for p in parts]
    if len(parts) == 2:
        return parts[0], parts[1]
    return "dbo", parts[-1]


def make_sandbox(cursor, proc_name: str, version: int) -> str:
    """Clone the proc into <schema>.<proc>_opt_v<n> so the live object is never
    touched. Handles schema-qualified and bracketed identifiers robustly so the
    sandbox is created in the SAME schema as the original."""
    original = get_proc_text(cursor, proc_name)
    if not original:
        raise RuntimeError(f"Could not read definition of {proc_name}")
    schema, short = _split_schema_proc(proc_name)
    sandbox_short = f"{short}_opt_v{version}"
    sandbox_name = f"[{schema}].[{sandbox_short}]"

    m = _PROC_HEADER_RE.search(original)
    if not m:
        raise RuntimeError(f"Could not locate CREATE/ALTER PROCEDURE header in {proc_name}")
    # Force CREATE (the live ALTER target would otherwise be rewritten in place)
    body = (
        original[: m.start()]
        + f"CREATE PROCEDURE {sandbox_name}"
        + original[m.end():]
    )
    cursor.execute(f"IF OBJECT_ID('{schema}.{sandbox_short}') IS NOT NULL DROP PROCEDURE {sandbox_name};")
    cursor.execute(body)
    return f"{schema}.{sandbox_short}"


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
    ap.add_argument(
        "--conn",
        default=os.environ.get("SQL_CONNECTION_STRING"),
        help="pyodbc connection string (defaults to SQL_CONNECTION_STRING env var)",
    )
    ap.add_argument("--backend", choices=["gemini", "claude", "file"], default="gemini")
    ap.add_argument("--model", default=None)
    ap.add_argument("--project", default=None, help="GCP project (gemini backend)")
    ap.add_argument("--decisions", default=os.environ.get("SP_OPT_DECISIONS"),
                    help="JSON file of pre-decided changes (file backend)")
    ap.add_argument("--max-iterations", type=int, default=5)
    ap.add_argument("--target-fraction", type=float, default=0.8)
    ap.add_argument("--quality-threshold", type=float, default=75.0)
    ap.add_argument("--max-combos", type=int, default=12)
    ap.add_argument("--actual", action="store_true",
                    help="run ACTUAL plans (executes proc — non-prod only)")
    ap.add_argument("--report", default="report.md")
    args = ap.parse_args(argv)

    if not args.conn:
        ap.error("--conn is required (or set SQL_CONNECTION_STRING in .env / environment)")

    try:
        import pyodbc
    except ImportError:
        print("pyodbc is required: pip install pyodbc", file=sys.stderr)
        return 2

    if args.backend == "gemini":
        backend = GeminiBackend(model=args.model or "gemini-1.5-flash", project=args.project)
    elif args.backend == "file":
        if not args.decisions:
            ap.error("--decisions <path> is required for --backend file (or set SP_OPT_DECISIONS)")
        backend = FileBackend(args.decisions)
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

    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    write_report(history, args.report, proc_name=args.proc)
    print(f"\nDone. {len(history)} iterations. Report: {args.report}")
    return 0


def _fmt(v, suffix=""):
    return f"{v:,.0f}{suffix}" if isinstance(v, (int, float)) else "—"


def write_report(history: list[IterationResult], path: str, proc_name: str = ""):
    lines = ["# SP Optimization Report\n"]
    if proc_name:
        lines.append(f"**Procedure:** `{proc_name}`\n")

    # ---- summary: baseline vs final ----
    if history:
        base = history[0]
        final = history[-1]
        lines.append("## Summary")
        lines.append(f"- Iterations run: **{len(history)}**")
        lines.append(f"- Baseline aggregate score (iter 0): **{base.aggregate_score:.1f}** "
                     f"({base.fraction_good:.0%} of combos good)")
        lines.append(f"- Final aggregate score (iter {final.iteration}): **{final.aggregate_score:.1f}** "
                     f"({final.fraction_good:.0%} of combos good)")
        if base.aggregate_score > 0:
            delta = (final.aggregate_score - base.aggregate_score) / base.aggregate_score * 100.0
            lines.append(f"- Aggregate improvement: **{delta:+.1f}%**")
        lines.append("")

    for r in history:
        lines.append(f"## Iteration {r.iteration}")
        lines.append(f"- Aggregate workload score: **{r.aggregate_score:.1f}**")
        lines.append(f"- Fraction good (score ≥ 75): **{r.fraction_good:.0%}**")
        if r.change_applied:
            c = r.change_applied
            lines.append(f"- Change applied: **{c.kind}** — `{c.target_object}`")
            lines.append(f"  - Rationale / citation: {c.rationale}")
            if c.apply_sql.strip():
                lines.append("  - Apply SQL:\n\n```sql\n" + c.apply_sql.strip() + "\n```")
            if c.rollback_sql.strip():
                lines.append("  - Rollback SQL:\n\n```sql\n" + c.rollback_sql.strip() + "\n```")
        if r.regressions:
            lines.append(f"- Regressions: {', '.join(r.regressions)}")

        lines.append("\n### Per-combo plan scores & runtime stats\n")
        lines.append("| Combo | Score | Elapsed (ms) | CPU (ms) | Logical reads | Rows out | Warnings |")
        lines.append("|---|---:|---:|---:|---:|---:|---|")
        for s in r.scores:
            warn = "; ".join(s.warnings) if s.warnings else ""
            lines.append(
                f"| `{s.combo_label}` | {s.score:.1f} | {_fmt(s.elapsed_ms)} | {_fmt(s.cpu_ms)} | "
                f"{_fmt(s.logical_reads)} | {_fmt(s.output_rows)} | {warn} |"
            )
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())
