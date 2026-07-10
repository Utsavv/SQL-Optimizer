"""Agent-driven driver: run the optimization loop one STEP at a time.

`optimize.py` runs the whole discover → capture → analyze → decide → apply →
verify loop as a single process, and its decision step calls a model through an
`LLMBackend` (LiteLLM API key, or a pre-staged `FileBackend` JSON). This module
is the alternative for when a **coding agent** (Claude Code, Codex, ...) is
already the brain: it exposes each deterministic step as its own CLI command so
the agent runs them in turn and makes the ONE decision itself, in its own
reasoning, between `evaluate` and `apply`.

There is therefore **no `LLMBackend` here at all** — no LiteLLM, no API key, and
no pre-staged decisions file. The agent reads the analysis `evaluate` prints,
decides the single smallest-safe change (grounded in `references/` and the
Microsoft Learn MCP, exactly as `SKILL.md` describes), and hands it back to
`apply`. The deterministic engine (`discover`, `capture`, `analyze`, `evidence`)
is reused verbatim; only the decision step moves out of the process and into the
agent.

Because each step is a separate process, the loop state (the workload, the
current sandbox variant, the iteration counter, and the per-iteration history)
is persisted to a ``session.json`` inside the run folder between commands.

Commands
--------
    discover  --proc P --conn C [tuning flags]   → start a run, derive workload
    evaluate  --session S --conn C               → capture+analyze current variant
    apply     --session S --conn C --change J    → apply the agent's chosen change
    finish    --session S                        → write report + winner + manifest
    status    --session S                        → print where the run stands

Typical agent loop:
    discover → evaluate → (agent decides) → apply → evaluate → ... → finish
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

# Load .env from the project root (two levels up from this file), same as
# optimize.py, so SQL_CONNECTION_STRING is picked up automatically.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent.parent / ".env")
except ImportError:
    pass

from . import analyze, capture, discover
from .evidence import RunDir
from .models import (
    Change,
    IterationResult,
    ParamCombo,
    PlanScore,
    fraction_good,
    workload_score,
)
# make_sandbox / get_proc_text / _split_batches / write_report are the sandbox +
# reporting primitives the monolithic loop already uses; reuse them verbatim so
# the agent-driven path produces byte-identical evidence, sandboxes and reports.
from .optimize import (
    get_proc_text,
    make_sandbox,
    write_report,
    _split_batches,
    _split_schema_proc,
)


SESSION_VERSION = 1


# ---- (de)serialization of the loop state -----------------------------------
#
# Each step is a fresh process, so the whole loop state round-trips through
# session.json. These helpers convert the in-memory dataclasses to/from plain
# JSON so evaluate/apply/finish can rebuild exactly what the monolithic loop
# holds in memory.

def _combo_to_dict(c: ParamCombo) -> dict:
    return {"values": c.values, "label": c.label, "weight": c.weight}


def _combo_from_dict(d: dict) -> ParamCombo:
    return ParamCombo(values=d["values"], label=d.get("label", ""),
                      weight=float(d.get("weight", 1.0)))


def _score_to_dict(s: PlanScore) -> dict:
    return {
        "combo_label": s.combo_label,
        "score": s.score,
        "warnings": s.warnings,
        "missing_indexes": s.missing_indexes,
        "signals": s.signals,
        "elapsed_ms": s.elapsed_ms,
        "cpu_ms": s.cpu_ms,
        "logical_reads": s.logical_reads,
        "output_rows": s.output_rows,
        "plan_path": s.plan_path,
        "stats_path": s.stats_path,
    }


def _score_from_dict(d: dict) -> PlanScore:
    return PlanScore(
        combo_label=d["combo_label"],
        score=d["score"],
        warnings=list(d.get("warnings", [])),
        missing_indexes=list(d.get("missing_indexes", [])),
        signals=dict(d.get("signals", {})),
        elapsed_ms=d.get("elapsed_ms"),
        cpu_ms=d.get("cpu_ms"),
        logical_reads=d.get("logical_reads"),
        output_rows=d.get("output_rows"),
        plan_path=d.get("plan_path"),
        stats_path=d.get("stats_path"),
    )


def _change_to_dict(c: Optional[Change]) -> Optional[dict]:
    if not c:
        return None
    return {
        "kind": c.kind,
        "rationale": c.rationale,
        "apply_sql": c.apply_sql,
        "rollback_sql": c.rollback_sql,
        "target_object": c.target_object,
    }


def _change_from_dict(d: dict) -> Change:
    return Change(
        kind=d.get("kind", "none"),
        rationale=d.get("rationale", ""),
        apply_sql=d.get("apply_sql", ""),
        rollback_sql=d.get("rollback_sql", ""),
        target_object=d.get("target_object", ""),
    )


def _iter_to_dict(r: IterationResult) -> dict:
    return {
        "iteration": r.iteration,
        "aggregate_score": r.aggregate_score,
        "fraction_good": r.fraction_good,
        "scored_proc": r.scored_proc,
        "proc_def": r.proc_def,
        "regressions": r.regressions,
        "change_applied": _change_to_dict(r.change_applied),
        "scores": [_score_to_dict(s) for s in r.scores],
    }


def _iter_from_dict(d: dict) -> IterationResult:
    r = IterationResult(
        iteration=d["iteration"],
        scores=[_score_from_dict(s) for s in d.get("scores", [])],
        aggregate_score=d["aggregate_score"],
        fraction_good=d["fraction_good"],
        scored_proc=d.get("scored_proc", ""),
        proc_def=d.get("proc_def", ""),
    )
    r.regressions = list(d.get("regressions", []))
    ch = d.get("change_applied")
    r.change_applied = _change_from_dict(ch) if ch else None
    return r


# ---- the persisted session --------------------------------------------------

class Session:
    """The loop state for one agent-driven run, persisted to session.json."""

    def __init__(self, data: dict, path: Path):
        self.path = path
        self.version = data.get("version", SESSION_VERSION)
        self.proc = data["proc"]
        self.run_root = data["run_root"]
        self.config = data["config"]
        self.combos = [_combo_from_dict(c) for c in data["combos"]]
        self.current_proc = data["current_proc"]
        self.iteration = data.get("iteration", 0)
        self.prev_aggregate = data.get("prev_aggregate", -1.0)
        self.stall_streak = data.get("stall_streak", 0)
        self.awaiting_apply = data.get("awaiting_apply", False)
        self.status = data.get("status", "open")
        self.history = [_iter_from_dict(h) for h in data.get("history", [])]

    # -- construction / persistence ------------------------------------------

    @classmethod
    def create(cls, run: RunDir, proc: str, combos: list[ParamCombo], config: dict) -> "Session":
        path = Path(run.root) / "session.json"
        data = {
            "version": SESSION_VERSION,
            "proc": proc,
            "run_root": str(run.root),
            "config": config,
            "combos": [_combo_to_dict(c) for c in combos],
            "current_proc": proc,
            "iteration": 0,
            "prev_aggregate": -1.0,
            "stall_streak": 0,
            "awaiting_apply": False,
            "status": "open",
            "history": [],
        }
        self = cls(data, path)
        self.save()
        return self

    @classmethod
    def load(cls, path: str) -> "Session":
        p = Path(path)
        if p.is_dir():
            p = p / "session.json"
        with open(p) as f:
            return cls(json.load(f), p)

    def save(self) -> None:
        data = {
            "version": SESSION_VERSION,
            "proc": self.proc,
            "run_root": self.run_root,
            "config": self.config,
            "combos": [_combo_to_dict(c) for c in self.combos],
            "current_proc": self.current_proc,
            "iteration": self.iteration,
            "prev_aggregate": self.prev_aggregate,
            "stall_streak": self.stall_streak,
            "awaiting_apply": self.awaiting_apply,
            "status": self.status,
            "history": [_iter_to_dict(h) for h in self.history],
        }
        self.path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def reopen_rundir(self) -> RunDir:
        return RunDir.reopen(self.run_root, self.proc)


# ---- step logic (engine calls; no model) -----------------------------------

def do_evaluate(session: Session, cursor) -> dict:
    """Capture + analyze the current variant, persist evidence, append the
    iteration, and decide whether a termination condition is met.

    Returns the decision context the AGENT reads to propose (or decline) a
    change — the same analysis the monolithic loop would have handed to an
    ``LLMBackend``, except here it goes to stdout for the agent to reason over.
    """
    if session.status != "open":
        raise SystemExit("session is finished; nothing more to evaluate")
    if session.awaiting_apply:
        raise SystemExit(
            "the last evaluate proposed a change that has not been applied yet — "
            "run `apply` (or `finish` to stop here) before evaluating again"
        )

    run = session.reopen_rundir()
    cfg = session.config
    it = session.iteration
    use_actual = bool(cfg.get("use_actual", False))
    quality_threshold = float(cfg.get("quality_threshold", 75.0))
    target_fraction = float(cfg.get("target_fraction", 0.8))
    max_iterations = int(cfg.get("max_iterations", 5))

    proc_def = get_proc_text(cursor, session.current_proc)
    mode = "actual" if use_actual else "estimated"
    run.log(f"[iter {it}] capture ({mode}) of {session.current_proc} "
            f"across {len(session.combos)} combo(s)")
    caps = capture.capture_workload(cursor, session.current_proc, session.combos, actual=use_actual)
    scores = analyze.analyze_workload(caps)

    for ordinal, (cap, score) in enumerate(zip(caps, scores)):
        plan_rel, stats_rel = run.write_evidence(it, ordinal, cap, score)
        score.plan_path = plan_rel
        score.stats_path = stats_rel

    agg = workload_score(scores, session.combos)
    frac = fraction_good(scores, quality_threshold)
    result = IterationResult(
        iteration=it, scores=scores, aggregate_score=agg, fraction_good=frac,
        scored_proc=session.current_proc, proc_def=proc_def,
    )
    session.history.append(result)
    run.log(f"[iter {it}] analyze · aggregate={agg:.1f} · "
            f"{frac:.0%} of combos ≥ {quality_threshold:.0f}")

    # --- termination (mirrors optimize.run_loop) ---
    stop_reason = None
    if frac >= target_fraction:
        stop_reason = "target_met"
    else:
        if agg > session.prev_aggregate + 0.5:
            session.stall_streak = 0
        else:
            session.stall_streak += 1
        if it > 0 and session.stall_streak >= 2:
            stop_reason = "stalled"
        elif it + 1 >= max_iterations:
            # No verify step remains after another apply — stop here.
            stop_reason = "max_iterations"
    session.prev_aggregate = agg
    session.awaiting_apply = stop_reason is None
    session.save()

    if stop_reason:
        run.log(f"[iter {it}] STOP suggested — {stop_reason} "
                f"(agg={agg:.1f}, {frac:.0%} good)")
    run.close()

    # The sandbox name the NEXT `apply` will create is deterministic; surface it
    # so a rewrite/recompile change can target it by name in its apply_sql
    # (e.g. ALTER PROCEDURE <next_sandbox> ... OPTION (RECOMPILE)).
    schema, short = _split_schema_proc(session.proc)
    next_sandbox = None if stop_reason else f"{schema}.{short}_opt_v{it + 1}"

    return {
        "iteration": it,
        "scored_proc": session.current_proc,
        "aggregate_score": round(agg, 2),
        "fraction_good": round(frac, 4),
        "quality_threshold": quality_threshold,
        "target_fraction": target_fraction,
        "stop_suggested": stop_reason is not None,
        "stop_reason": stop_reason,
        "next_step": "finish" if stop_reason else "apply (propose one smallest-safe change) or finish",
        "next_sandbox": next_sandbox,
        "procedure_definition": proc_def,
        "combos": [
            {
                "combo": s.combo_label,
                "score": round(s.score, 1),
                "warnings": s.warnings,
                "missing_indexes": s.missing_indexes,
                "signals": s.signals,
                "runtime": {
                    "elapsed_ms": s.elapsed_ms,
                    "cpu_ms": s.cpu_ms,
                    "logical_reads": s.logical_reads,
                    "output_rows": s.output_rows,
                },
                "evidence": {"plan": s.plan_path, "statistics": s.stats_path},
            }
            for s in scores
        ],
    }


def do_apply(session: Session, cursor, change: Change) -> dict:
    """Apply the agent's chosen change to a fresh sandbox copy and advance the
    loop. The change is decided by the AGENT, not by any model in this process."""
    if session.status != "open":
        raise SystemExit("session is finished; nothing more to apply")
    if not session.awaiting_apply:
        raise SystemExit(
            "no evaluated iteration is awaiting a change — run `evaluate` first"
        )
    if change.kind == "none" or not change.apply_sql.strip():
        raise SystemExit(
            "change is kind='none' or has empty apply_sql — there is nothing to "
            "apply; call `finish` to stop the run instead"
        )

    run = session.reopen_rundir()
    it = session.iteration
    result = session.history[-1]
    result.change_applied = change
    run.log(f"[iter {it}] decide · {change.kind} on {change.target_object}: "
            f"{change.rationale}", echo=False)

    sandbox = make_sandbox(cursor, session.proc, it + 1)
    try:
        for stmt in _split_batches(change.apply_sql):
            if stmt.strip():
                cursor.execute(stmt)
    except Exception as e:
        result.regressions.append(f"apply failed: {e}")
        session.awaiting_apply = False
        session.save()
        run.log(f"[iter {it}] apply FAILED on sandbox {sandbox}: {e}")
        run.close()
        raise SystemExit(
            f"apply failed on sandbox {sandbox}: {e}\n"
            f"The failure is recorded on iteration {it}. Call `finish` to write "
            f"the report, or fix the change and re-run `evaluate`."
        )

    session.current_proc = sandbox
    session.iteration = it + 1
    session.awaiting_apply = False
    session.save()
    run.log(f"[iter {it}] applied {change.kind}: {change.target_object} → "
            f"sandbox {sandbox} (agg={result.aggregate_score:.1f})")
    run.close()

    return {
        "applied": True,
        "iteration": it,
        "kind": change.kind,
        "target_object": change.target_object,
        "sandbox": sandbox,
        "next_step": "evaluate (re-verify the sandbox across the same workload)",
    }


def do_finish(session: Session) -> dict:
    """Write the end-of-run artifacts (changes.sql, winner.sql, manifest.json,
    report.html) from the accumulated history and close the session."""
    if not session.history:
        raise SystemExit("no iterations recorded yet — run `evaluate` at least once")

    run = session.reopen_rundir()
    run.write_changes(session.history)
    best = run.write_winner(session.history)
    run.write_manifest(session.history, session.proc)
    report_path = run.report_path
    write_report(session.history, str(report_path), proc_name=session.proc, run=run)

    session.status = "final"
    session.awaiting_apply = False
    session.save()
    run.log(f"run complete · {len(session.history)} iteration(s) · "
            f"winner=iter {best.iteration if best else '—'} · report={report_path}")
    run.close()

    return {
        "status": "final",
        "iterations": len(session.history),
        "winner_iteration": best.iteration if best else None,
        "run_folder": str(run.root),
        "report": str(report_path),
        "changes": str(run.changes_path),
        "winner": str(run.winner_path),
        "manifest": str(run.manifest_path),
        "log": str(run.log_path),
    }


# ---- CLI --------------------------------------------------------------------

def _connect(conn: str):
    try:
        import pyodbc
    except ImportError:
        print("pyodbc is required: pip install pyodbc", file=sys.stderr)
        raise SystemExit(2)
    connection = pyodbc.connect(conn, autocommit=True)
    return connection, connection.cursor()


def _load_change(args) -> Change:
    """Build the agent's Change from --change <json-file> or inline flags."""
    if args.change:
        with open(args.change) as f:
            raw = f.read().strip()
        data = json.loads(raw)
        # Accept either a single change object or a 1-element array.
        if isinstance(data, list):
            if len(data) != 1:
                raise SystemExit("--change file must hold exactly ONE change object "
                                 "(the agent applies one smallest-safe change per step)")
            data = data[0]
        return _change_from_dict(data)
    if not args.kind:
        raise SystemExit("provide --change <json-file> or the inline --kind/--apply-sql flags")
    return Change(
        kind=args.kind,
        rationale=args.rationale or "",
        apply_sql=args.apply_sql or "",
        rollback_sql=args.rollback_sql or "",
        target_object=args.target_object or "",
    )


def _emit(obj: dict) -> None:
    """Print a step result as a JSON block the agent can parse from stdout."""
    print(json.dumps(obj, indent=2, default=str))


def _cmd_discover(args) -> int:
    conn = args.conn or os.environ.get("SQL_CONNECTION_STRING")
    if not conn:
        raise SystemExit("--conn is required (or set SQL_CONNECTION_STRING in .env)")
    _, cursor = _connect(conn)
    params, combos = discover.discover(cursor, args.proc, max_combos=args.max_combos)
    run = RunDir(args.out_dir, args.proc)
    config = {
        "target_fraction": args.target_fraction,
        "quality_threshold": args.quality_threshold,
        "max_iterations": args.max_iterations,
        "regression_tolerance": args.regression_tolerance,
        "use_actual": args.actual,
        "max_combos": args.max_combos,
    }
    session = Session.create(run, args.proc, combos, config)
    run.log(f"discover · {len(params)} param(s), {len(combos)} workload combo(s): "
            + ", ".join(c.label or "default" for c in combos))
    run.close()
    _emit({
        "session": str(session.path),
        "run_folder": str(run.root),
        "proc": args.proc,
        "params": [{"name": p.name, "type": p.sql_type,
                    "has_default": p.has_default, "is_output": p.is_output}
                   for p in params],
        "combos": [{"label": c.label or "default", "values": c.values, "weight": c.weight}
                   for c in combos],
        "next_step": "evaluate --session <session> --conn <conn>",
    })
    return 0


def _cmd_evaluate(args) -> int:
    conn = args.conn or os.environ.get("SQL_CONNECTION_STRING")
    if not conn:
        raise SystemExit("--conn is required (or set SQL_CONNECTION_STRING in .env)")
    session = Session.load(args.session)
    _, cursor = _connect(conn)
    _emit(do_evaluate(session, cursor))
    return 0


def _cmd_apply(args) -> int:
    conn = args.conn or os.environ.get("SQL_CONNECTION_STRING")
    if not conn:
        raise SystemExit("--conn is required (or set SQL_CONNECTION_STRING in .env)")
    session = Session.load(args.session)
    change = _load_change(args)
    _, cursor = _connect(conn)
    _emit(do_apply(session, cursor, change))
    return 0


def _cmd_finish(args) -> int:
    session = Session.load(args.session)
    _emit(do_finish(session))
    return 0


def _cmd_status(args) -> int:
    session = Session.load(args.session)
    last = session.history[-1] if session.history else None
    _emit({
        "proc": session.proc,
        "run_folder": session.run_root,
        "status": session.status,
        "current_proc": session.current_proc,
        "iterations_evaluated": len(session.history),
        "awaiting_apply": session.awaiting_apply,
        "last_aggregate_score": round(last.aggregate_score, 2) if last else None,
        "last_fraction_good": round(last.fraction_good, 4) if last else None,
        "combos": len(session.combos),
    })
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scripts.session",
        description="Agent-driven SP optimizer: run the loop one step at a time "
                    "(the agent makes the decision; no LiteLLM / API key needed).",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="start a run and derive the workload")
    d.add_argument("--proc", required=True, help="schema-qualified proc name")
    d.add_argument("--conn", default=None,
                   help="pyodbc connection string (defaults to SQL_CONNECTION_STRING)")
    d.add_argument("--out-dir", default="out",
                   help="base output dir; the run lands in <out-dir>/<schema.proc>/<timestamp>/")
    d.add_argument("--max-iterations", type=int, default=5)
    d.add_argument("--target-fraction", type=float, default=0.8)
    d.add_argument("--quality-threshold", type=float, default=75.0)
    d.add_argument("--regression-tolerance", type=float, default=10.0)
    d.add_argument("--max-combos", type=int, default=12)
    d.add_argument("--actual", action="store_true",
                   help="run ACTUAL plans (executes proc — non-prod only)")
    d.set_defaults(func=_cmd_discover)

    e = sub.add_parser("evaluate", help="capture + analyze the current variant")
    e.add_argument("--session", required=True, help="session.json (or its run folder)")
    e.add_argument("--conn", default=None)
    e.set_defaults(func=_cmd_evaluate)

    a = sub.add_parser("apply", help="apply the agent's chosen change to a sandbox")
    a.add_argument("--session", required=True, help="session.json (or its run folder)")
    a.add_argument("--conn", default=None)
    a.add_argument("--change", default=None,
                   help="JSON file with {kind, rationale, apply_sql, rollback_sql, target_object}")
    a.add_argument("--kind", default=None, help="inline change kind (alternative to --change)")
    a.add_argument("--rationale", default=None)
    a.add_argument("--apply-sql", default=None)
    a.add_argument("--rollback-sql", default=None)
    a.add_argument("--target-object", default=None)
    a.set_defaults(func=_cmd_apply)

    f = sub.add_parser("finish", help="write the report + winner + manifest and close")
    f.add_argument("--session", required=True, help="session.json (or its run folder)")
    f.set_defaults(func=_cmd_finish)

    s = sub.add_parser("status", help="print where the run stands")
    s.add_argument("--session", required=True, help="session.json (or its run folder)")
    s.set_defaults(func=_cmd_status)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
