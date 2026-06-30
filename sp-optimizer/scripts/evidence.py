"""Run-directory + evidence persistence for the optimizer loop.

Every optimization run gets its OWN folder, namespaced by procedure and
timestamp, so multiple runs (of the same or different procs) never collide and
each run is fully self-contained:

    <base>/<schema.proc>/<YYYY-MM-DD_HHMMSS>/
        report.html             # the human report (links into evidence/)
        run.log                 # structured, timestamped step-by-step log
        changes.sql             # every applied change + its rollback
        winner.sql              # the best-performing procedure variant
        manifest.json           # machine-readable index of the whole run
        evidence/
            iter0/
                00-narrow-last-1-day.plan.xml        # execution plan (XML)
                00-narrow-last-1-day.statistics.txt  # SET STATISTICS IO/TIME
                00-narrow-last-1-day.score.json      # analysis + signals
                ...
            iter1/
                ...

The point is that EVERY piece of evidence captured at EVERY step is written to
disk and referenced from the final report — nothing the loop reasoned over is
left only in memory.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import Change, IterationResult, PlanCapture, PlanScore


def _slug(text: str, fallback: str = "item") -> str:
    """Filesystem-safe slug from an arbitrary label."""
    s = re.sub(r"[^0-9A-Za-z]+", "-", (text or "").strip()).strip("-").lower()
    return s or fallback


def _proc_slug(proc_name: str) -> str:
    """Folder name for a proc: keep the readable ``schema.proc`` shape, strip
    brackets, and replace anything filesystem-unfriendly with an underscore."""
    s = re.sub(r"[\[\]]", "", proc_name or "proc")
    s = re.sub(r"[^0-9A-Za-z._]+", "_", s).strip("._")
    return s or "proc"


class RunDir:
    """Owns one run's folder and writes every artifact + evidence file into it."""

    def __init__(self, base: str, proc_name: str, when: Optional[datetime] = None):
        when = when or datetime.now()
        self.proc_name = proc_name
        self.timestamp = when
        self.stamp = when.strftime("%Y-%m-%d_%H%M%S")
        self.root = Path(base) / _proc_slug(proc_name) / self.stamp
        self.evidence_root = self.root / "evidence"
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        self.report_path = self.root / "report.html"
        self.changes_path = self.root / "changes.sql"
        self.winner_path = self.root / "winner.sql"
        self.manifest_path = self.root / "manifest.json"
        self.log_path = self.root / "run.log"
        self._log_fh = open(self.log_path, "a", encoding="utf-8")
        self.log(f"run start · proc={proc_name} · dir={self.root}")

    # ---- logging ------------------------------------------------------------

    def log(self, msg: str, echo: bool = True) -> None:
        """Append a timestamped line to run.log (and, by default, the console)."""
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
        self._log_fh.write(line + "\n")
        self._log_fh.flush()
        if echo:
            print(msg)

    # ---- per-combo evidence -------------------------------------------------

    def write_evidence(
        self, iteration: int, ordinal: int, cap: PlanCapture, score: Optional[PlanScore] = None
    ) -> tuple[Optional[str], Optional[str]]:
        """Persist all evidence for one combo in one iteration: the execution
        plan XML, the IO/TIME statistics text, the analysis (score + signals),
        and any capture error. Returns ``(plan_rel, stats_rel)`` paths relative
        to the run root so the report can link to them directly."""
        it_dir = self.evidence_root / f"iter{iteration}"
        it_dir.mkdir(parents=True, exist_ok=True)
        base = f"{ordinal:02d}-{_slug(cap.combo.label, 'combo')}"

        plan_rel: Optional[str] = None
        stats_rel: Optional[str] = None

        if cap.plan_xml:
            p = it_dir / f"{base}.plan.xml"
            p.write_text(cap.plan_xml, encoding="utf-8")
            plan_rel = str(p.relative_to(self.root))

        if cap.io_stats_text:
            p = it_dir / f"{base}.statistics.txt"
            p.write_text(cap.io_stats_text, encoding="utf-8")
            stats_rel = str(p.relative_to(self.root))

        if cap.error:
            (it_dir / f"{base}.error.txt").write_text(cap.error, encoding="utf-8")

        if score is not None:
            payload = {
                "combo": cap.combo.label,
                "values": cap.combo.values,
                "weight": cap.combo.weight,
                "score": round(score.score, 2),
                "warnings": score.warnings,
                "missing_indexes": score.missing_indexes,
                "signals": score.signals,
                "runtime": {
                    "elapsed_ms": score.elapsed_ms,
                    "cpu_ms": score.cpu_ms,
                    "logical_reads": score.logical_reads,
                    "output_rows": score.output_rows,
                },
                "evidence": {"plan": plan_rel, "statistics": stats_rel},
            }
            (it_dir / f"{base}.score.json").write_text(
                json.dumps(payload, indent=2, default=str), encoding="utf-8"
            )

        return plan_rel, stats_rel

    # ---- end-of-run artifacts ----------------------------------------------

    def write_changes(self, history: list[IterationResult]) -> None:
        """Write every applied change with its apply + rollback SQL, in order."""
        lines = [
            f"-- Applied changes for {self.proc_name}",
            f"-- Run: {self.stamp}",
            "-- The live procedure was NEVER modified; changes apply to sandbox",
            "-- copies (<proc>_opt_v<n>). Apply blocks are in order; rollback",
            "-- blocks reverse them in any order.",
            "",
        ]
        any_change = False
        for r in history:
            c = r.change_applied
            if not c or c.kind == "none" or not c.apply_sql.strip():
                continue
            any_change = True
            lines += [
                f"-- ===== Iteration {r.iteration}: {c.kind} {c.target_object} =====",
                f"-- {c.rationale}",
                "",
                c.apply_sql.strip(),
                "",
                "-- Rollback:",
                _comment_block(c.rollback_sql.strip()),
                "",
            ]
        if not any_change:
            lines.append("-- No changes were applied during this run.")
        self.changes_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def write_winner(self, history: list[IterationResult]) -> Optional[IterationResult]:
        """Write the best-scoring procedure variant + the changes that produced
        it. Returns the winning IterationResult (or None if there's no history)."""
        if not history:
            return None
        best = max(history, key=lambda r: r.aggregate_score)
        baseline = history[0]
        header = [
            f"-- Winning variant for {self.proc_name}",
            f"-- Run: {self.stamp}",
            f"-- Baseline (iter {baseline.iteration}) aggregate score: "
            f"{baseline.aggregate_score:.1f}",
            f"-- Winner   (iter {best.iteration}) aggregate score: "
            f"{best.aggregate_score:.1f}  ({best.fraction_good:.0%} of combos good)",
            f"-- Scored object: {best.scored_proc or self.proc_name}",
            "",
            "-- Changes that produced this winner (apply in order):",
        ]
        # A change recorded on iteration N is applied AFTER iteration N is scored
        # (it produces the sandbox scored in iteration N+1). So the changes that
        # produced the winning definition are those from iterations strictly
        # BEFORE best.iteration; including best.iteration's own change would list
        # a change that is not present in the winning proc_def.
        applied = [
            r.change_applied for r in history
            if r.iteration < best.iteration
            and r.change_applied and r.change_applied.kind != "none"
            and r.change_applied.apply_sql.strip()
        ]
        if applied:
            for c in applied:
                header += ["", f"-- {c.kind}: {c.target_object} — {c.rationale}",
                           c.apply_sql.strip()]
        else:
            header.append("-- (none — the baseline procedure already met the bar)")
        header += ["", "-- Best-performing procedure definition:", ""]
        body = best.proc_def.strip() or "-- (procedure definition unavailable)"
        self.winner_path.write_text("\n".join(header) + "\n" + body + "\n", encoding="utf-8")
        return best

    def write_manifest(self, history: list[IterationResult], proc_name: str) -> None:
        """A machine-readable index of the whole run: every iteration, every
        combo, and the evidence path for each, so the run can be consumed by
        tooling without re-parsing the HTML."""
        iterations = []
        for r in history:
            combos = [
                {
                    "combo": s.combo_label,
                    "score": round(s.score, 2),
                    "warnings": s.warnings,
                    "plan": s.plan_path,
                    "statistics": s.stats_path,
                }
                for s in r.scores
            ]
            iterations.append({
                "iteration": r.iteration,
                "aggregate_score": round(r.aggregate_score, 2),
                "fraction_good": round(r.fraction_good, 4),
                "scored_proc": r.scored_proc,
                "change_applied": _change_dict(r.change_applied),
                "regressions": r.regressions,
                "combos": combos,
            })
        manifest = {
            "proc": proc_name,
            "run": self.stamp,
            "generated": datetime.now().isoformat(timespec="seconds"),
            "artifacts": {
                "report": self.report_path.name,
                "run_log": self.log_path.name,
                "changes": self.changes_path.name,
                "winner": self.winner_path.name,
            },
            "iterations": iterations,
        }
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2, default=str), encoding="utf-8"
        )

    def close(self) -> None:
        try:
            self._log_fh.close()
        except Exception:
            pass


def _comment_block(sql: str) -> str:
    """Render SQL as a leading-dash comment block (used for rollback in changes.sql)."""
    if not sql:
        return "-- (no rollback provided)"
    return "\n".join("-- " + ln for ln in sql.splitlines())


def _change_dict(c: Optional[Change]) -> Optional[dict]:
    if not c:
        return None
    return {
        "kind": c.kind,
        "target_object": c.target_object,
        "rationale": c.rationale,
        "apply_sql": c.apply_sql,
        "rollback_sql": c.rollback_sql,
    }
