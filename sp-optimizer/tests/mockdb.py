"""Tiny mock pyodbc-style cursor for the deterministic-engine tests.

Real SQL Server is never available in CI, so the tests drive discovery/capture
against a scripted cursor: ``execute(sql, *params)`` matches ``sql`` against a
list of (substring, rows) rules and stashes the result for ``fetchall`` /
``fetchone``. Rows are attribute-accessible (like pyodbc.Row).
"""
from __future__ import annotations

from typing import Any


class Row:
    """Attribute- and index-accessible row, like pyodbc.Row."""

    def __init__(self, **cols: Any):
        self._cols = cols
        for k, v in cols.items():
            setattr(self, k, v)

    def __getitem__(self, i):
        return list(self._cols.values())[i]

    def __len__(self):
        return len(self._cols)


class MockCursor:
    """Scripted cursor. ``rules`` is a list of (substring, rows) — the first whose
    substring appears in the executed SQL supplies the next fetch result. Unknown
    SQL yields an empty result set. ``timeout`` is accepted and recorded."""

    def __init__(self, rules=None):
        self.rules = list(rules or [])
        self._pending: list = []
        self.executed: list[str] = []
        self.timeout = None
        self._messages: list = []

    def add_rule(self, substring: str, rows: list):
        self.rules.append((substring, rows))
        return self

    def execute(self, sql, *params):
        self.executed.append(sql)
        for substring, rows in self.rules:
            if substring.lower() in sql.lower():
                self._pending = list(rows)
                return self
        self._pending = []
        return self

    def fetchall(self):
        out, self._pending = self._pending, []
        return out

    def fetchone(self):
        if self._pending:
            return self._pending.pop(0)
        return None

    def nextset(self):
        return False

    @property
    def messages(self):
        return self._messages
