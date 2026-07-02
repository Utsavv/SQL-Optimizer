"""Deterministic guardrails for LLM-proposed index changes.

The SKILL safety rules say an index is a permanent write tax and must never be
created blind. The decision prompt asks the model to check overlap and state
cost — but a prompt is a request, not a guarantee. These checks run BEFORE any
``kind="index"`` change is applied:

  1. **Overlap**: a proposed index whose key columns are a left-prefix of (or
     identical to) an existing index's keys is redundant — REJECTED, and the
     rejection is fed back to the decision step via the attempt history.
  2. **Size estimate**: table row count × summed key/include column widths,
     surfaced in the log and report so the space cost is visible up front.
  3. **Write tax**: cumulative updates against the table since restart (from
     sys.dm_db_index_usage_stats), showing how hot the write path is that the
     new index will slow down.

Every DB read degrades gracefully — a guardrail that cannot gather evidence
reports what it could not check instead of blocking the run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProposedIndex:
    """A CREATE INDEX statement, parsed."""
    name: str
    table: str
    key_columns: list[str] = field(default_factory=list)
    include_columns: list[str] = field(default_factory=list)


_CREATE_INDEX_RE = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?(?:NONCLUSTERED\s+|CLUSTERED\s+)?INDEX\s+"
    r"(?P<name>\[[^\]]+\]|\w+)\s+ON\s+"
    r"(?P<table>(?:\[[^\]]+\]|\w+)(?:\s*\.\s*(?:\[[^\]]+\]|\w+))*)\s*"
    r"\((?P<keys>[^)]*)\)"
    r"(?:\s*INCLUDE\s*\((?P<incl>[^)]*)\))?",
    re.IGNORECASE,
)


def _clean_cols(raw: str) -> list[str]:
    """'[Col A] ASC, ColB DESC' -> ['col a', 'colb'] (order preserved)."""
    cols = []
    for part in raw.split(","):
        part = re.sub(r"\b(ASC|DESC)\b", "", part, flags=re.IGNORECASE)
        part = part.strip().strip("[]").strip()
        if part:
            cols.append(part.lower())
    return cols


def parse_index_ddl(sql: str) -> Optional[ProposedIndex]:
    """Extract the first CREATE INDEX from a change's apply_sql, or None."""
    m = _CREATE_INDEX_RE.search(sql or "")
    if not m:
        return None
    return ProposedIndex(
        name=m.group("name").strip("[]"),
        table=re.sub(r"[\[\]\s]", "", m.group("table")),
        key_columns=_clean_cols(m.group("keys") or ""),
        include_columns=_clean_cols(m.group("incl") or ""),
    )


def is_left_prefix(candidate: list[str], existing: list[str]) -> bool:
    """True when ``candidate`` equals the first len(candidate) keys of ``existing``."""
    return (
        0 < len(candidate) <= len(existing)
        and existing[: len(candidate)] == candidate
    )


def check_overlap(
    proposed: ProposedIndex,
    existing: dict[str, list[str]],
) -> tuple[bool, list[str]]:
    """Compare proposed key columns against existing index keys on the table.

    Returns (ok, notes). ok=False means the proposal is redundant (its keys
    are a left-prefix of, or identical to, an existing index) and must be
    rejected. A proposal that EXTENDS an existing index is allowed but noted,
    so the reviewer can consider replacing the narrower index."""
    notes: list[str] = []
    for name, keys in existing.items():
        if is_left_prefix(proposed.key_columns, keys):
            notes.append(
                f"REJECTED: key columns ({', '.join(proposed.key_columns)}) are a "
                f"left-prefix of existing index '{name}' ({', '.join(keys)}) — the "
                f"existing index already provides this access path"
            )
            return False, notes
        if is_left_prefix(keys, proposed.key_columns):
            notes.append(
                f"note: proposed index extends existing '{name}' ({', '.join(keys)}) — "
                f"consider dropping/replacing the narrower index to avoid overlap"
            )
    return True, notes


# ---- cursor-facing evidence gathering ----------------------------------------

_EXISTING_INDEXES_SQL = """
SELECT i.name AS index_name, c.name AS col_name, ic.key_ordinal
FROM sys.indexes i
JOIN sys.index_columns ic
  ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c
  ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE i.object_id = OBJECT_ID(?)
  AND i.name IS NOT NULL
  AND ic.is_included_column = 0
ORDER BY i.name, ic.key_ordinal;
"""

_ROWCOUNT_SQL = """
SELECT SUM(p.row_count) AS row_count
FROM sys.dm_db_partition_stats p
WHERE p.object_id = OBJECT_ID(?) AND p.index_id IN (0, 1);
"""

_COL_WIDTH_SQL = """
SELECT c.name AS col_name, c.max_length
FROM sys.columns c
WHERE c.object_id = OBJECT_ID(?);
"""

_WRITE_ACTIVITY_SQL = """
SELECT SUM(s.user_updates) AS updates, MAX(s.last_user_update) AS last_update
FROM sys.dm_db_index_usage_stats s
WHERE s.database_id = DB_ID() AND s.object_id = OBJECT_ID(?);
"""


def existing_index_keys(cursor, table: str) -> dict[str, list[str]]:
    """{index_name: [key columns in order]} for a table; {} on any failure."""
    try:
        cursor.execute(_EXISTING_INDEXES_SQL, table)
        out: dict[str, list[str]] = {}
        for row in cursor.fetchall():
            out.setdefault(row.index_name, []).append(row.col_name.lower())
        return out
    except Exception:
        return {}


def estimate_index_size_mb(cursor, proposed: ProposedIndex) -> Optional[float]:
    """rows × summed column widths, in MB. Rough — LOB/varchar(max) columns are
    assumed at 100 bytes — but the right order of magnitude for a go/no-go."""
    try:
        cursor.execute(_ROWCOUNT_SQL, proposed.table)
        row = cursor.fetchone()
        rows = int(row.row_count) if row and row.row_count is not None else None
        if rows is None:
            return None
        cursor.execute(_COL_WIDTH_SQL, proposed.table)
        widths = {r.col_name.lower(): int(r.max_length) for r in cursor.fetchall()}
        per_row = 7  # row header + slot overhead
        for col in proposed.key_columns + proposed.include_columns:
            w = widths.get(col, 8)
            per_row += 100 if w < 0 else w  # -1 = varchar(max)/LOB
        return rows * per_row / (1024.0 * 1024.0)
    except Exception:
        return None


def table_write_activity(cursor, table: str) -> Optional[int]:
    """Cumulative user updates against the table since instance restart."""
    try:
        cursor.execute(_WRITE_ACTIVITY_SQL, table)
        row = cursor.fetchone()
        return int(row.updates) if row and row.updates is not None else None
    except Exception:
        return None


def check_index_change(cursor, apply_sql: str) -> tuple[bool, list[str]]:
    """Full guardrail pass for a kind='index' change. Returns (ok, notes).

    ok=False only for a hard failure (redundant overlap). Size / write-tax
    evidence is returned as notes either way so it lands in the log and the
    attempt history."""
    proposed = parse_index_ddl(apply_sql)
    if proposed is None:
        return True, ["no CREATE INDEX statement found in apply_sql — overlap/size "
                      "checks skipped"]
    ok, notes = check_overlap(proposed, existing_index_keys(cursor, proposed.table))
    if not ok:
        return False, notes

    size_mb = estimate_index_size_mb(cursor, proposed)
    if size_mb is not None:
        notes.append(f"estimated size ≈ {size_mb:,.1f} MB "
                     f"({len(proposed.key_columns)} key / "
                     f"{len(proposed.include_columns)} included column(s))")
    else:
        notes.append("size estimate unavailable")

    updates = table_write_activity(cursor, proposed.table)
    if updates is not None:
        notes.append(f"write tax: {updates:,} update(s) against {proposed.table} "
                     f"since instance restart will now also maintain this index")
    return True, notes
