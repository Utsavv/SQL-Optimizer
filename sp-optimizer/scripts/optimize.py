"""Step 4–6 orchestration: the closed optimization loop + CLI.

Wires discover -> capture -> analyze -> decide -> apply -> verify and repeats
until a termination condition is met. Everything is applied to a SANDBOX copy
of the procedure; the live object is never modified.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# Load .env from the project root (two levels up from this file).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent.parent / ".env")
except ImportError:
    pass  # python-dotenv not installed; rely on environment variables directly

from . import analyze, capture, discover
from .llm import FileBackend, LiteLLMBackend, LLMBackend
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
    ap.add_argument("--backend", choices=["litellm", "file"], default="litellm")
    ap.add_argument(
        "--model", default=None,
        help="LiteLLM model string, e.g. gemini/gemini-1.5-flash, "
             "claude-3-5-sonnet-20241022, gpt-4o (defaults to LLM_MODEL in .env)",
    )
    ap.add_argument("--decisions", default=os.environ.get("SP_OPT_DECISIONS"),
                    help="JSON file of pre-decided changes (file backend)")
    ap.add_argument("--max-iterations", type=int, default=5)
    ap.add_argument("--target-fraction", type=float, default=0.8)
    ap.add_argument("--quality-threshold", type=float, default=75.0)
    ap.add_argument("--max-combos", type=int, default=12)
    ap.add_argument("--actual", action="store_true",
                    help="run ACTUAL plans (executes proc — non-prod only)")
    ap.add_argument("--report", default="report.html")
    args = ap.parse_args(argv)

    if not args.conn:
        ap.error("--conn is required (or set SQL_CONNECTION_STRING in .env / environment)")

    try:
        import pyodbc
    except ImportError:
        print("pyodbc is required: pip install pyodbc", file=sys.stderr)
        return 2

    if args.backend == "file":
        if not args.decisions:
            ap.error("--decisions <path> is required for --backend file (or set SP_OPT_DECISIONS)")
        backend = FileBackend(args.decisions)
    else:
        backend = LiteLLMBackend(model=args.model)

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


# ---- HTML report ------------------------------------------------------------
#
# The report is a single self-contained HTML file (CSS inlined, no external
# assets or network calls) so it can be opened straight from disk or emailed.
# It is organized to answer three questions at a glance: what changed, why it
# changed, and what was tried across the loop.

_SCORE_THRESHOLD = 75.0  # the "good plan" bar used throughout the report


def _esc(v) -> str:
    """HTML-escape arbitrary values, rendering None as an em dash."""
    if v is None:
        return "—"
    return html.escape(str(v), quote=True)


def _score_class(score: float) -> str:
    """Bucket a 0..100 score into a CSS class for color-coding."""
    if score >= _SCORE_THRESHOLD:
        return "good"
    if score >= 50.0:
        return "warn"
    return "bad"


_KIND_LABELS = {
    "index": "Index",
    "option_hint": "Query hint",
    "recompile": "Recompile",
    "rewrite": "Rewrite",
    "none": "No change",
}


def _kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, kind.replace("_", " ").title() if kind else "—")


def _delta_badge(base: float, final: float) -> str:
    """A signed-percentage badge describing the baseline → final movement."""
    if base <= 0:
        return '<span class="badge neutral">n/a</span>'
    delta = (final - base) / base * 100.0
    cls = "good" if delta > 0.5 else "bad" if delta < -0.5 else "neutral"
    arrow = "▲" if delta > 0.5 else "▼" if delta < -0.5 else "■"
    return f'<span class="badge {cls}">{arrow} {delta:+.1f}%</span>'


def _score_bar(score: float) -> str:
    pct = max(0.0, min(100.0, score))
    return (
        f'<div class="bar"><div class="bar-fill {_score_class(score)}" '
        f'style="width:{pct:.0f}%"></div></div>'
    )


def _render_summary(history: list[IterationResult], proc_name: str) -> str:
    base, final = history[0], history[-1]
    cards = [
        ("Iterations run", f"{len(history)}", ""),
        ("Baseline score",
         f"{base.aggregate_score:.1f}",
         f"{base.fraction_good:.0%} of combos good"),
        ("Final score",
         f"{final.aggregate_score:.1f}",
         f"{final.fraction_good:.0%} of combos good"),
        ("Improvement",
         _delta_badge(base.aggregate_score, final.aggregate_score),
         f"aggregate, iter 0 → iter {final.iteration}"),
    ]
    card_html = "\n".join(
        f'<div class="stat"><div class="stat-label">{_esc(label)}</div>'
        f'<div class="stat-value">{value}</div>'
        f'<div class="stat-sub">{_esc(sub)}</div></div>'
        for label, value, sub in cards
    )
    return f"""
    <section class="summary card" id="summary">
      <h2>Summary</h2>
      <div class="stat-grid">{card_html}</div>
      <div class="progress-pair">
        <div class="progress-row">
          <span class="progress-name">Baseline (iter 0)</span>
          {_score_bar(base.aggregate_score)}
          <span class="progress-num {_score_class(base.aggregate_score)}">{base.aggregate_score:.1f}</span>
        </div>
        <div class="progress-row">
          <span class="progress-name">Final (iter {final.iteration})</span>
          {_score_bar(final.aggregate_score)}
          <span class="progress-num {_score_class(final.aggregate_score)}">{final.aggregate_score:.1f}</span>
        </div>
      </div>
    </section>"""


def _render_timeline(history: list[IterationResult]) -> str:
    """A compact 'what was tried' rail across every iteration."""
    items = []
    for r in history:
        if r.change_applied and r.change_applied.kind != "none":
            c = r.change_applied
            dot = "applied"
            head = f"{_esc(_kind_label(c.kind))}"
            if c.target_object:
                head += f' <code>{_esc(c.target_object)}</code>'
            detail = _esc(c.rationale)
        elif r.regressions:
            dot = "failed"
            head = "Stopped — regression"
            detail = _esc("; ".join(r.regressions))
        else:
            dot = "none"
            head = "No change applied"
            detail = "Proc already good enough, stalled, or no safe change proposed."
        items.append(
            f'<li class="tl-item {dot}">'
            f'<div class="tl-marker"></div>'
            f'<div class="tl-body">'
            f'<div class="tl-head"><a href="#iter-{r.iteration}">Iteration {r.iteration}</a> '
            f'<span class="score-pill {_score_class(r.aggregate_score)}">{r.aggregate_score:.1f}</span> '
            f'<span class="muted">{r.fraction_good:.0%} good</span></div>'
            f'<div class="tl-title">{head}</div>'
            f'<div class="tl-detail">{detail}</div>'
            f'</div></li>'
        )
    return f"""
    <section class="card" id="tried">
      <h2>What was tried</h2>
      <ul class="timeline">{''.join(items)}</ul>
    </section>"""


def _render_change(c: Change) -> str:
    blocks = [
        '<div class="change">',
        f'<div class="change-head"><span class="kind kind-{_esc(c.kind)}">{_esc(_kind_label(c.kind))}</span>',
    ]
    if c.target_object:
        blocks.append(f'<code class="target">{_esc(c.target_object)}</code>')
    blocks.append('</div>')
    blocks.append(
        f'<div class="rationale"><span class="why">Why</span>{_esc(c.rationale)}</div>'
    )
    if c.apply_sql.strip():
        blocks.append(
            '<details open class="sql"><summary>Apply SQL</summary>'
            f'<pre><code>{_esc(c.apply_sql.strip())}</code></pre></details>'
        )
    if c.rollback_sql.strip():
        blocks.append(
            '<details class="sql"><summary>Rollback SQL</summary>'
            f'<pre><code>{_esc(c.rollback_sql.strip())}</code></pre></details>'
        )
    blocks.append('</div>')
    return "".join(blocks)


def _render_iteration(r: IterationResult) -> str:
    parts = [
        f'<section class="card iteration" id="iter-{r.iteration}">',
        '<div class="iter-head">',
        f'<h2>Iteration {r.iteration}</h2>',
        f'<div class="iter-stats">'
        f'<span class="score-pill big {_score_class(r.aggregate_score)}">{r.aggregate_score:.1f}</span>'
        f'<span class="muted">aggregate · {r.fraction_good:.0%} of combos ≥ {_SCORE_THRESHOLD:.0f}</span>'
        f'</div>',
        '</div>',
    ]

    if r.change_applied and r.change_applied.kind != "none":
        parts.append(_render_change(r.change_applied))
    else:
        parts.append('<p class="muted no-change">No change applied this iteration.</p>')

    if r.regressions:
        regs = "".join(f"<li>{_esc(x)}</li>" for x in r.regressions)
        parts.append(f'<div class="regressions"><strong>Regressions</strong><ul>{regs}</ul></div>')

    # per-combo table
    rows = []
    for s in r.scores:
        warn = "; ".join(s.warnings) if s.warnings else ""
        rows.append(
            f'<tr><td><code>{_esc(s.combo_label)}</code></td>'
            f'<td class="num"><span class="score-pill {_score_class(s.score)}">{s.score:.1f}</span></td>'
            f'<td class="num">{_esc(_fmt(s.elapsed_ms))}</td>'
            f'<td class="num">{_esc(_fmt(s.cpu_ms))}</td>'
            f'<td class="num">{_esc(_fmt(s.logical_reads))}</td>'
            f'<td class="num">{_esc(_fmt(s.output_rows))}</td>'
            f'<td class="warn-cell">{_esc(warn) if warn else ""}</td></tr>'
        )
    parts.append(
        '<h3>Per-combo plan scores &amp; runtime stats</h3>'
        '<div class="table-wrap"><table>'
        '<thead><tr><th>Combo</th><th class="num">Score</th><th class="num">Elapsed (ms)</th>'
        '<th class="num">CPU (ms)</th><th class="num">Logical reads</th>'
        '<th class="num">Rows out</th><th>Warnings</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )
    parts.append('</section>')
    return "".join(parts)


_REPORT_CSS = """
:root {
  --bg: #f4f6fb; --fg: #1b2230; --muted: #6b7585; --card: #ffffff;
  --border: #e3e8f0; --accent: #3b5bdb; --accent-soft: #eef2ff;
  --good: #1f9d55; --good-bg: #e6f6ec; --warn: #b7791f; --warn-bg: #fdf4e3;
  --bad: #d64545; --bad-bg: #fdecec; --code-bg: #0f172a; --code-fg: #e2e8f0;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
code, pre { font-family: "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace; }
.page { max-width: 1040px; margin: 0 auto; padding: 0 20px 64px; }
header.top {
  background: linear-gradient(135deg, #3b5bdb, #5f3dc4); color: #fff;
  padding: 32px 24px; border-radius: 0 0 16px 16px; margin-bottom: 24px;
}
header.top h1 { margin: 0 0 6px; font-size: 24px; letter-spacing: -0.2px; }
header.top .proc { font-size: 15px; opacity: .92; }
header.top .proc code { background: rgba(255,255,255,.18); padding: 2px 8px; border-radius: 6px; }
header.top .generated { margin-top: 10px; font-size: 12.5px; opacity: .8; }
nav.jump {
  position: sticky; top: 0; z-index: 5; background: rgba(244,246,251,.92);
  backdrop-filter: blur(6px); padding: 10px 0; margin-bottom: 18px;
  border-bottom: 1px solid var(--border); display: flex; gap: 8px; flex-wrap: wrap;
}
nav.jump a {
  text-decoration: none; color: var(--accent); font-size: 13px; font-weight: 500;
  padding: 4px 10px; border-radius: 999px; background: var(--accent-soft);
}
nav.jump a:hover { background: #dde4ff; }
.card {
  background: var(--card); border: 1px solid var(--border); border-radius: 14px;
  padding: 22px 24px; margin-bottom: 20px; box-shadow: 0 1px 2px rgba(16,24,40,.04);
}
.card h2 { margin: 0 0 16px; font-size: 18px; }
.card h3 { margin: 22px 0 10px; font-size: 14px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
.muted { color: var(--muted); }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; }
.stat { background: var(--accent-soft); border-radius: 10px; padding: 14px 16px; }
.stat-label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
.stat-value { font-size: 26px; font-weight: 700; margin: 4px 0 2px; }
.stat-sub { font-size: 12.5px; color: var(--muted); }
.progress-pair { margin-top: 18px; display: flex; flex-direction: column; gap: 10px; }
.progress-row { display: grid; grid-template-columns: 130px 1fr 48px; align-items: center; gap: 12px; }
.progress-name { font-size: 13px; color: var(--muted); }
.progress-num { text-align: right; font-weight: 700; font-variant-numeric: tabular-nums; }
.bar { height: 10px; background: #eceff4; border-radius: 999px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 999px; }
.bar-fill.good { background: var(--good); }
.bar-fill.warn { background: var(--warn); }
.bar-fill.bad { background: var(--bad); }
.badge { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 14px; font-weight: 700; }
.badge.good { background: var(--good-bg); color: var(--good); }
.badge.bad { background: var(--bad-bg); color: var(--bad); }
.badge.neutral { background: #eef1f6; color: var(--muted); }
.score-pill { display: inline-block; min-width: 38px; text-align: center; padding: 2px 8px; border-radius: 8px; font-weight: 700; font-variant-numeric: tabular-nums; }
.score-pill.big { font-size: 18px; padding: 4px 12px; }
.score-pill.good { background: var(--good-bg); color: var(--good); }
.score-pill.warn { background: var(--warn-bg); color: var(--warn); }
.score-pill.bad { background: var(--bad-bg); color: var(--bad); }
.timeline { list-style: none; margin: 0; padding: 0; }
.tl-item { display: grid; grid-template-columns: 22px 1fr; gap: 6px; padding-bottom: 18px; position: relative; }
.tl-item:not(:last-child)::before { content: ""; position: absolute; left: 9px; top: 18px; bottom: 0; width: 2px; background: var(--border); }
.tl-marker { width: 14px; height: 14px; border-radius: 50%; margin-top: 4px; border: 3px solid #fff; box-shadow: 0 0 0 1px var(--border); }
.tl-item.applied .tl-marker { background: var(--good); }
.tl-item.failed .tl-marker { background: var(--bad); }
.tl-item.none .tl-marker { background: #c2c9d6; }
.tl-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.tl-head a { font-weight: 600; text-decoration: none; color: var(--accent); }
.tl-title { font-weight: 600; margin: 2px 0; }
.tl-title code { background: var(--accent-soft); padding: 1px 6px; border-radius: 5px; }
.tl-detail { font-size: 13.5px; color: var(--muted); }
.iter-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.iter-head h2 { margin: 0; }
.iter-stats { display: flex; align-items: center; gap: 10px; }
.change { border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; background: #fbfcfe; margin-bottom: 4px; }
.change-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
.kind { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; padding: 3px 10px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); }
.target { background: #eef1f6; padding: 2px 8px; border-radius: 6px; font-size: 13px; }
.rationale { font-size: 14px; }
.rationale .why { display: inline-block; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); margin-right: 8px; vertical-align: 1px; }
details.sql { margin-top: 12px; }
details.sql summary { cursor: pointer; font-size: 12.5px; font-weight: 600; color: var(--accent); user-select: none; }
details.sql pre { background: var(--code-bg); color: var(--code-fg); padding: 14px 16px; border-radius: 8px; overflow-x: auto; font-size: 13px; line-height: 1.5; margin: 8px 0 0; }
.no-change { font-style: italic; }
.regressions { background: var(--bad-bg); border-radius: 10px; padding: 10px 16px; margin-top: 12px; color: var(--bad); }
.regressions ul { margin: 6px 0 0; padding-left: 18px; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--border); }
th { font-size: 11.5px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tbody tr:hover { background: #f8faff; }
.warn-cell { color: var(--warn); font-size: 12.5px; }
footer { text-align: center; color: var(--muted); font-size: 12.5px; margin-top: 24px; }
@media (max-width: 600px) {
  .progress-row { grid-template-columns: 1fr; gap: 4px; }
  .iter-head { align-items: flex-start; }
}
"""


def _render_html(history: list[IterationResult], proc_name: str) -> str:
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    proc_line = (
        f'<div class="proc">Procedure: <code>{_esc(proc_name)}</code></div>'
        if proc_name else ""
    )

    if not history:
        body = '<section class="card"><h2>No iterations recorded</h2>' \
               '<p class="muted">The optimization loop produced no results.</p></section>'
        nav = ""
    else:
        nav_links = ['<a href="#summary">Summary</a>', '<a href="#tried">What was tried</a>']
        nav_links += [f'<a href="#iter-{r.iteration}">Iter {r.iteration}</a>' for r in history]
        nav = f'<nav class="jump">{"".join(nav_links)}</nav>'
        body = (
            _render_summary(history, proc_name)
            + _render_timeline(history)
            + "".join(_render_iteration(r) for r in history)
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SP Optimization Report{(' — ' + _esc(proc_name)) if proc_name else ''}</title>
<style>{_REPORT_CSS}</style>
</head>
<body>
<header class="top">
  <h1>SP Optimization Report</h1>
  {proc_line}
  <div class="generated">Generated {generated}</div>
</header>
<div class="page">
{nav}
{body}
<footer>Generated by sp-optimizer · the live procedure was never modified (changes apply to sandbox copies).</footer>
</div>
</body>
</html>"""


def write_report(history: list[IterationResult], path: str, proc_name: str = ""):
    """Write the optimization run as a self-contained HTML report."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(_render_html(history, proc_name))


if __name__ == "__main__":
    raise SystemExit(main())
