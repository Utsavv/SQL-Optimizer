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

from . import analyze, capture, discover, guardrails, review
from .evidence import RunDir
from .llm import FileBackend, LiteLLMBackend, LLMBackend
from .models import (
    AttemptRecord,
    Change,
    DecisionContext,
    IterationResult,
    PlanScore,
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


def make_sandbox(cursor, source_proc: str, base_proc: str, version: int) -> str:
    """Clone ``source_proc`` into <schema>.<base>_opt_v<n> so the live object is
    never touched. Cloning from the CURRENT variant (not always the original)
    lets changes compound across iterations: a body rewrite applied in v1
    survives into v2 when v2 only adds an index. Naming stays anchored to the
    original proc so every sandbox is recognisable and CLEANUP.sql can drop
    them. Handles schema-qualified and bracketed identifiers robustly so the
    sandbox is created in the SAME schema as the original."""
    original = get_proc_text(cursor, source_proc)
    if not original:
        raise RuntimeError(f"Could not read definition of {source_proc}")
    schema, base_short = _split_schema_proc(base_proc)
    sandbox_short = f"{base_short}_opt_v{version}"
    sandbox_name = f"[{schema}].[{sandbox_short}]"

    m = _PROC_HEADER_RE.search(original)
    if not m:
        raise RuntimeError(f"Could not locate CREATE/ALTER PROCEDURE header in {source_proc}")
    # Force CREATE (the live ALTER target would otherwise be rewritten in place)
    body = (
        original[: m.start()]
        + f"CREATE PROCEDURE {sandbox_name}"
        + original[m.end():]
    )
    cursor.execute(f"IF OBJECT_ID('{schema}.{sandbox_short}') IS NOT NULL DROP PROCEDURE {sandbox_name};")
    cursor.execute(body)
    return f"{schema}.{sandbox_short}"


def drop_sandbox(cursor, sandbox: str) -> None:
    """Drop a sandbox proc that ended up unused (decision was 'none'/rejected).
    Refuses any name that isn't a *_opt_v<n> sandbox — this function must never
    be able to touch the live procedure, whatever the caller passes. Best-effort
    otherwise: a leftover empty sandbox is harmless and CLEANUP.sql catches it."""
    try:
        schema, short = _split_schema_proc(sandbox)
        if not re.search(r"_opt_v\d+$", short):
            return
        cursor.execute(
            f"IF OBJECT_ID('{schema}.{short}') IS NOT NULL DROP PROCEDURE [{schema}].[{short}];"
        )
    except Exception:
        pass


def _run_sql_batches(cursor, sql: str) -> None:
    for stmt in _split_batches(sql):
        if stmt.strip():
            cursor.execute(stmt)


def _try_rollback(cursor, change: Change, run: RunDir, why: str) -> bool:
    """Execute a change's rollback SQL. Returns True when it ran cleanly."""
    if not change.rollback_sql.strip():
        run.log(f"rollback SKIPPED — {change.kind} {change.target_object} has no "
                f"rollback SQL ({why}); CLEANUP.sql is the fallback")
        return False
    try:
        _run_sql_batches(cursor, change.rollback_sql)
        run.log(f"rolled back {change.kind} {change.target_object} ({why})")
        return True
    except Exception as e:
        run.log(f"rollback FAILED for {change.kind} {change.target_object}: {e}")
        return False


def _combo_regressions(
    prev_scores: list[PlanScore], cur_scores: list[PlanScore], tolerance: float
) -> list[str]:
    """Per-combo regressions beyond tolerance vs. the previous iteration.

    This is the verify gate the termination conditions promise: a change that
    lifts the aggregate while tanking one combo is a regression, not a win."""
    prev_by = {s.combo_label: s.score for s in prev_scores}
    out = []
    for s in cur_scores:
        p = prev_by.get(s.combo_label)
        if p is not None and p - s.score > tolerance:
            out.append(
                f"combo '{s.combo_label}' regressed {p:.1f} → {s.score:.1f} "
                f"(drop {p - s.score:.1f} > tolerance {tolerance:.1f})"
            )
    return out


# ---- the loop ---------------------------------------------------------------

def run_loop(
    cursor,
    proc_name: str,
    backend: LLMBackend,
    run: RunDir,
    max_iterations: int = 5,
    target_fraction: float = 0.8,
    quality_threshold: float = 75.0,
    regression_tolerance: float = 10.0,
    use_actual: bool = False,
    max_combos: int = 12,
    auto_rollback: bool = True,
    runs_per_combo: int = 1,
    allow_plan_forcing: bool = False,
) -> list[IterationResult]:
    params, combos = discover.discover(cursor, proc_name, max_combos=max_combos)
    run.log(
        f"discover · {len(params)} param(s), {len(combos)} workload combo(s): "
        + ", ".join(c.label or "default" for c in combos)
    )

    # Static review of the ORIGINAL procedure, once per run. Deterministic and
    # read-only; findings ground the decision step and appear in the report.
    findings = review.review_procedure(
        cursor, proc_name, get_proc_text(cursor, proc_name), params
    )
    run.write_review(findings)
    if findings:
        run.log(f"review · {len(findings)} finding(s): "
                + ", ".join(f"{f.severity}:{f.rule}" for f in findings[:8])
                + ("…" if len(findings) > 8 else ""))
        for f in findings:
            run.log(f"review ·   [{f.severity}] {f.rule}: {f.message}", echo=False)
    else:
        run.log("review · no static findings")

    # Query Store plan history is only offered to the decision step when the
    # user explicitly allowed plan forcing: forcing changes LIVE query behavior
    # (it is not sandboxed), so it stays opt-in.
    qs_plans: list[dict] = []
    if allow_plan_forcing:
        qs_plans = discover.query_store_plan_summary(cursor, proc_name)
        if qs_plans:
            run.log(f"query store · {len(qs_plans)} plan(s) available for forcing")

    history: list[IterationResult] = []
    attempts: list[AttemptRecord] = []
    current_proc = proc_name
    prev_result: IterationResult | None = None   # last iteration kept (not rolled back)
    pending_change: Change | None = None         # change applied after prev_result
    pending_result: IterationResult | None = None  # iteration that applied it
    prev_aggregate = -1.0
    stall_streak = 0
    version = 0

    for it in range(max_iterations):
        proc_def = get_proc_text(cursor, current_proc)
        mode = "actual" if use_actual else "estimated"
        run.log(f"[iter {it}] capture ({mode}) of {current_proc} across {len(combos)} combo(s)")
        caps = capture.capture_workload(cursor, current_proc, combos,
                                        actual=use_actual, runs=runs_per_combo)
        scores = analyze.analyze_workload(caps)

        # Persist every piece of evidence for this iteration and link it onto
        # the scores so the report can point straight at the raw plan / IO stats.
        for ordinal, (cap, score) in enumerate(zip(caps, scores)):
            plan_rel, stats_rel = run.write_evidence(it, ordinal, cap, score)
            score.plan_path = plan_rel
            score.stats_path = stats_rel
            note = "ok" if not cap.error else f"ERROR: {cap.error}"
            run.log(
                f"[iter {it}]   combo '{cap.combo.label or 'default'}' "
                f"score={score.score:.1f} plan={plan_rel or '—'} "
                f"stats={stats_rel or '—'} ({note})",
                echo=False,
            )

        agg = workload_score(scores, combos)
        frac = fraction_good(scores, quality_threshold, combos)

        result = IterationResult(
            iteration=it, scores=scores, aggregate_score=agg, fraction_good=frac,
            scored_proc=current_proc, proc_def=proc_def,
        )
        run.log(f"[iter {it}] analyze · aggregate={agg:.1f} · {frac:.0%} of combos ≥ {quality_threshold:.0f}")

        # --- verify gate: did the change applied last iteration regress a combo? ---
        regressed = False
        if prev_result is not None and pending_change is not None:
            regs = _combo_regressions(prev_result.scores, scores, regression_tolerance)
            if regs:
                regressed = True
                result.regressions = regs
                result.variant_invalidated = True
                for r_msg in regs:
                    run.log(f"[iter {it}] verify · {r_msg}")
                _try_rollback(cursor, pending_change, run, "per-combo regression")
                pending_result.change_rolled_back = True
                attempts.append(AttemptRecord(
                    iteration=pending_result.iteration,
                    kind=pending_change.kind,
                    target_object=pending_change.target_object,
                    outcome="rolled_back",
                    detail=f"aggregate {prev_result.aggregate_score:.1f} → {agg:.1f}; "
                           + "; ".join(regs),
                ))
            elif pending_result is not None:
                attempts.append(AttemptRecord(
                    iteration=pending_result.iteration,
                    kind=pending_change.kind,
                    target_object=pending_change.target_object,
                    outcome="kept",
                    detail=f"aggregate {prev_result.aggregate_score:.1f} → {agg:.1f}",
                ))
        history.append(result)
        pending_change = None
        pending_result = None

        if regressed:
            # Revert the decision inputs to the last good variant — its scores
            # still describe it accurately, so no re-capture is needed.
            current_proc = prev_result.scored_proc
            proc_def = prev_result.proc_def
            scores = prev_result.scores
            agg = prev_result.aggregate_score
            frac = prev_result.fraction_good
            stall_streak += 1
        else:
            prev_result = result
            # termination: good enough (only a non-regressed iteration counts)
            if frac >= target_fraction:
                run.log(f"[iter {it}] STOP — target met: {frac:.0%} good, agg={agg:.1f}")
                break
            # stall bookkeeping (2 consecutive no-improvement rounds stop the loop)
            if agg > prev_aggregate + 0.5:
                stall_streak = 0
            else:
                stall_streak += 1

        if it > 0 and stall_streak >= 2:
            run.log(f"[iter {it}] STOP — stalled {stall_streak} consecutive round(s) "
                    f"(agg={agg:.1f} vs prev {prev_aggregate:.1f})")
            break

        # --- decision step ---------------------------------------------------
        # The sandbox is created BEFORE the decision so the model is told the
        # exact object its apply_sql must target — it never guesses the name.
        # Cloning from current_proc lets changes compound across iterations.
        version += 1
        sandbox = make_sandbox(cursor, current_proc, proc_name, version)
        context = DecisionContext(sandbox_proc=sandbox, attempts=list(attempts),
                                  review_findings=findings,
                                  query_store_plans=qs_plans)
        change = backend.propose_change(proc_def, scores, context)
        if change.kind == "none" or not change.apply_sql.strip():
            drop_sandbox(cursor, sandbox)
            run.log(f"[iter {it}] STOP — no safe change proposed")
            break
        run.log(f"[iter {it}] decide · {change.kind} on {change.target_object}: {change.rationale}", echo=False)

        # --- plan forcing acts on LIVE query behavior — hard-gated -----------
        if change.kind == "force_plan" and not allow_plan_forcing:
            attempts.append(AttemptRecord(
                iteration=it, kind=change.kind, target_object=change.target_object,
                outcome="rejected",
                detail="plan forcing not allowed for this run (--allow-plan-forcing)",
            ))
            drop_sandbox(cursor, sandbox)
            run.log(f"[iter {it}] REJECTED force_plan — run started without --allow-plan-forcing")
            stall_streak += 1
            if stall_streak >= 2:
                run.log(f"[iter {it}] STOP — {stall_streak} consecutive failed round(s)")
                break
            continue
        if change.kind == "force_plan":
            # Forcing needs no sandbox: it pins a plan for the live queries and
            # is verified by re-capturing the SAME proc next iteration.
            drop_sandbox(cursor, sandbox)
            sandbox = current_proc

        # --- guardrails: deterministic checks before an index is created ------
        if change.kind == "index":
            ok, notes = guardrails.check_index_change(cursor, change.apply_sql)
            for n in notes:
                run.log(f"[iter {it}] guardrail · {n}")
            if not ok:
                attempts.append(AttemptRecord(
                    iteration=it, kind=change.kind, target_object=change.target_object,
                    outcome="rejected", detail="; ".join(notes),
                ))
                drop_sandbox(cursor, sandbox)
                run.log(f"[iter {it}] REJECTED index {change.target_object} by guardrail")
                stall_streak += 1
                if stall_streak >= 2:
                    run.log(f"[iter {it}] STOP — {stall_streak} consecutive failed round(s)")
                    break
                continue
            if notes:
                change.rationale += " [guardrails: " + "; ".join(notes) + "]"

        # --- apply to the sandbox, verify next iteration ----------------------
        try:
            _run_sql_batches(cursor, change.apply_sql)
        except Exception as e:
            result.regressions.append(f"apply failed: {e}")
            attempts.append(AttemptRecord(
                iteration=it, kind=change.kind, target_object=change.target_object,
                outcome="failed", detail=f"apply error: {e}",
            ))
            _try_rollback(cursor, change, run, "apply failed part-way")
            drop_sandbox(cursor, sandbox)
            run.log(f"[iter {it}] apply FAILED on sandbox {sandbox}: {e}")
            stall_streak += 1
            if stall_streak >= 2:
                run.log(f"[iter {it}] STOP — {stall_streak} consecutive failed round(s)")
                break
            continue

        result.change_applied = change
        pending_change = change
        pending_result = result
        current_proc = sandbox
        prev_aggregate = agg
        run.log(f"[iter {it}] applied {change.kind}: {change.target_object} → sandbox {sandbox} (agg={agg:.1f})")

    # --- end of run: undo every change that is not part of the winner ---------
    # Changes recorded on iteration N produce the variant scored at N+1, so the
    # winner's definition contains only changes from iterations strictly before
    # it. Anything applied at/after the winning iteration (including a final,
    # never-verified change) has real side effects — indexes especially — and
    # is rolled back so the database ends the run carrying only the winner.
    if auto_rollback and history:
        candidates = [r for r in history if not r.variant_invalidated] or history
        best = max(candidates, key=lambda r: r.aggregate_score)
        losers = [
            r for r in history
            if r.change_applied and not r.change_rolled_back
            and r.change_applied.kind != "none"
            and r.iteration >= best.iteration
        ]
        for r in reversed(losers):
            _try_rollback(cursor, r.change_applied, run,
                          f"not part of winner (iter {best.iteration})")
            r.change_rolled_back = True

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
        help="LiteLLM model string, e.g. ollama_chat/gemma4, gemini/gemini-1.5-flash, "
             "claude-3-5-sonnet-20241022, gpt-4o (defaults to LLM_MODEL in .env, "
             "or ollama_chat/gemma4 against local Ollama if unset)",
    )
    ap.add_argument("--decisions", default=os.environ.get("SP_OPT_DECISIONS"),
                    help="JSON file of pre-decided changes (file backend)")
    ap.add_argument("--max-iterations", type=int, default=5)
    ap.add_argument("--target-fraction", type=float, default=0.8)
    ap.add_argument("--quality-threshold", type=float, default=75.0)
    ap.add_argument("--regression-tolerance", type=float, default=10.0,
                    help="max per-combo score drop a change may cause before it "
                         "is rolled back (default 10)")
    ap.add_argument("--max-combos", type=int, default=12)
    ap.add_argument("--actual", action="store_true",
                    help="run ACTUAL plans (executes proc — non-prod only)")
    ap.add_argument("--runs", type=int, default=1,
                    help="measured executions per combo in --actual mode (plus one "
                         "discarded warm-up); medians are reported. Default 1 — "
                         "use 3+ when timings matter")
    ap.add_argument("--query-timeout", type=int, default=300,
                    help="per-statement timeout in seconds (default 300) so a "
                         "runaway full-history combo cannot hang the run")
    ap.add_argument("--no-auto-rollback", action="store_true",
                    help="keep every applied change on the database at end of run "
                         "(default: changes not part of the winner are rolled back)")
    ap.add_argument("--allow-plan-forcing", action="store_true",
                    help="let the decision step propose Query Store plan forcing "
                         "(sp_query_store_force_plan). Affects LIVE query behavior "
                         "— it is not sandboxed — so it is opt-in")
    ap.add_argument("--out-dir", default="out",
                    help="base output dir; each run lands in "
                         "<out-dir>/<schema.proc>/<timestamp>/ (default: out)")
    ap.add_argument("--report", default=None,
                    help="report path override (default: report.html inside the run dir)")
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
    conn.timeout = args.query_timeout  # applies to every statement on this connection
    cursor = conn.cursor()

    # Each run gets its OWN folder: <out-dir>/<schema.proc>/<timestamp>/.
    run = RunDir(args.out_dir, args.proc)
    try:
        history = run_loop(
            cursor,
            args.proc,
            backend,
            run,
            max_iterations=args.max_iterations,
            target_fraction=args.target_fraction,
            quality_threshold=args.quality_threshold,
            regression_tolerance=args.regression_tolerance,
            use_actual=args.actual,
            max_combos=args.max_combos,
            auto_rollback=not args.no_auto_rollback,
            runs_per_combo=args.runs,
            allow_plan_forcing=args.allow_plan_forcing,
        )

        # End-of-run artifacts, all inside the run folder alongside the evidence.
        run.write_changes(history)
        best = run.write_winner(history)
        run.write_manifest(history, args.proc)

        report_path = Path(args.report) if args.report else run.report_path
        report_path.parent.mkdir(parents=True, exist_ok=True)
        write_report(history, str(report_path), proc_name=args.proc, run=run)

        run.log(
            f"run complete · {len(history)} iteration(s) · "
            f"winner=iter {best.iteration if best else '—'} · report={report_path}"
        )
        print(f"\nDone. {len(history)} iteration(s). Run folder: {run.root}")
        print(f"  report:  {report_path}")
        print(f"  log:     {run.log_path}")
        print(f"  changes: {run.changes_path}")
        print(f"  winner:  {run.winner_path}")
    finally:
        run.close()
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
            dot = "failed" if r.change_rolled_back else "applied"
            head = f"{_esc(_kind_label(c.kind))}"
            if c.target_object:
                head += f' <code>{_esc(c.target_object)}</code>'
            if r.change_rolled_back:
                head += ' <span class="muted">(rolled back)</span>'
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
        if r.change_rolled_back:
            parts.append('<p class="muted">The change below was <strong>rolled back</strong> — '
                         'its verify pass regressed a combo, or it was not part of the winner.</p>')
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
            f'<td class="evidence-cell">{_evidence_links(s)}</td>'
            f'<td class="warn-cell">{_esc(warn) if warn else ""}</td></tr>'
        )
    parts.append(
        '<h3>Per-combo plan scores, runtime stats &amp; evidence</h3>'
        '<div class="table-wrap"><table>'
        '<thead><tr><th>Combo</th><th class="num">Score</th><th class="num">Elapsed (ms)</th>'
        '<th class="num">CPU (ms)</th><th class="num">Logical reads</th>'
        '<th class="num">Rows out</th><th>Evidence</th><th>Warnings</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )
    parts.append('</section>')
    return "".join(parts)


def _evidence_links(s) -> str:
    """Links to the persisted plan XML / IO-stat text for one combo, if any."""
    links = []
    if s.plan_path:
        links.append(f'<a href="{_esc(s.plan_path)}">plan</a>')
    if s.stats_path:
        links.append(f'<a href="{_esc(s.stats_path)}">IO stats</a>')
    return " · ".join(links) if links else '<span class="muted">—</span>'


_SEVERITY_CLASS = {"high": "bad", "medium": "warn", "info": "neutral"}


def _render_review(run) -> str:
    """The static T-SQL review findings, when the run produced any."""
    if run is None or not getattr(run, "review_findings", None):
        return ""
    rows = []
    for f in run.review_findings:
        cls = _SEVERITY_CLASS.get(f.severity, "neutral")
        snippet = f'<code>{_esc(f.snippet)}</code>' if f.snippet else '<span class="muted">—</span>'
        rows.append(
            f'<tr><td><span class="badge {cls}">{_esc(f.severity)}</span></td>'
            f'<td><code>{_esc(f.rule)}</code></td>'
            f'<td>{_esc(f.message)}</td>'
            f'<td class="warn-cell">{snippet}</td></tr>'
        )
    return f"""
    <section class="card" id="review">
      <h2>Static T-SQL review</h2>
      <p class="muted">Deterministic linter findings on the procedure text —
      root causes the execution plan can only show symptoms of. Also fed to the
      decision step.</p>
      <div class="table-wrap"><table>
      <thead><tr><th>Severity</th><th>Rule</th><th>Finding</th><th>Snippet</th></tr></thead>
      <tbody>{''.join(rows)}</tbody></table></div>
    </section>"""


def _render_artifacts(run) -> str:
    """A section listing the run folder and every persisted artifact, so the
    reader knows exactly where the raw evidence lives on disk."""
    if run is None:
        return ""
    items = [
        ("Run folder", str(run.root)),
        ("Run log", run.log_path.name),
        ("Applied changes", run.changes_path.name),
        ("Winning variant", run.winner_path.name),
        ("Manifest (JSON)", run.manifest_path.name),
        ("Raw evidence", "evidence/iter&lt;n&gt;/&lt;combo&gt;.{plan.xml, statistics.txt, score.json}"),
    ]
    rows = "".join(
        f'<tr><td>{_esc(label)}</td><td><code>{val}</code></td></tr>'
        for label, val in items
    )
    return f"""
    <section class="card" id="artifacts">
      <h2>Evidence &amp; artifacts</h2>
      <p class="muted">Every plan and IO stat captured at every step is saved in this run folder
      and linked from the per-iteration tables above.</p>
      <div class="table-wrap"><table><tbody>{rows}</tbody></table></div>
    </section>"""


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
.evidence-cell { font-size: 12.5px; white-space: nowrap; }
.evidence-cell a { color: var(--accent); text-decoration: none; font-weight: 500; }
.evidence-cell a:hover { text-decoration: underline; }
#artifacts code { background: #eef1f6; padding: 2px 7px; border-radius: 6px; font-size: 12.5px; }
footer { text-align: center; color: var(--muted); font-size: 12.5px; margin-top: 24px; }
@media (max-width: 600px) {
  .progress-row { grid-template-columns: 1fr; gap: 4px; }
  .iter-head { align-items: flex-start; }
}
"""


def _render_html(history: list[IterationResult], proc_name: str, run=None) -> str:
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
        if run is not None and getattr(run, "review_findings", None):
            nav_links.append('<a href="#review">Review</a>')
        nav_links += [f'<a href="#iter-{r.iteration}">Iter {r.iteration}</a>' for r in history]
        if run is not None:
            nav_links.append('<a href="#artifacts">Evidence</a>')
        nav = f'<nav class="jump">{"".join(nav_links)}</nav>'
        body = (
            _render_summary(history, proc_name)
            + _render_timeline(history)
            + _render_review(run)
            + "".join(_render_iteration(r) for r in history)
            + _render_artifacts(run)
        )

    run_line = (
        f'<div class="generated">Run folder: <code>{_esc(str(run.root))}</code></div>'
        if run is not None else ""
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
  {run_line}
</header>
<div class="page">
{nav}
{body}
<footer>Generated by sp-optimizer · the live procedure was never modified (changes apply to sandbox copies).</footer>
</div>
</body>
</html>"""


def write_report(history: list[IterationResult], path: str, proc_name: str = "", run=None):
    """Write the optimization run as a self-contained HTML report.

    When a ``run`` (RunDir) is supplied, the report links to the per-combo plan
    XML / IO-stat evidence persisted under the run folder and lists every
    artifact written for the run."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(_render_html(history, proc_name, run=run))


if __name__ == "__main__":
    raise SystemExit(main())
