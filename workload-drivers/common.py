"""Shared helpers for the workload drivers.

Small, dependency-light utilities so each driver script stays focused on the
workload itself: resolving the connection string (CLI > env/.env), connecting
with pyodbc, and a thread-safe running-stats accumulator used to print live
throughput/latency while a run is in flight.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# Load .env from the repo root if python-dotenv is available. The drivers reuse
# the same SQL_CONNECTION_STRING the rest of the repo uses (see .env.example).
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # pragma: no cover - optional convenience only
    pass


def resolve_conn_str(cli_conn: Optional[str]) -> str:
    """Return the connection string, preferring an explicit --conn over the
    SQL_CONNECTION_STRING environment variable (populated from .env). Exits with
    a clear message if neither is set."""
    conn = cli_conn or os.environ.get("SQL_CONNECTION_STRING")
    if not conn:
        print(
            "ERROR: no connection string. Pass --conn or set SQL_CONNECTION_STRING "
            "in your .env (see .env.example).",
            file=sys.stderr,
        )
        sys.exit(1)
    return conn


def connect(conn_str: str, timeout: int = 30):
    """Open a pyodbc connection, failing loudly if pyodbc is missing."""
    try:
        import pyodbc
    except ImportError:
        print(
            "ERROR: pyodbc is not installed. Run: pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(1)
    return pyodbc.connect(conn_str, timeout=timeout)


def redact_conn(conn_str: str) -> str:
    """Best-effort redaction of a password for safe printing/logging."""
    parts = []
    for token in conn_str.split(";"):
        key = token.split("=", 1)[0].strip().upper()
        if key in ("PWD", "PASSWORD"):
            parts.append(f"{token.split('=', 1)[0]}=***")
        else:
            parts.append(token)
    return ";".join(parts)


class Stats:
    """Thread-safe accumulator for operation count + total latency.

    Mirrors the running totals the original C# driver kept under a lock, but
    also tracks errors and wall-clock start so we can report throughput
    (ops/sec) in addition to average latency.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.ops = 0
        self.total_ms = 0.0
        self.errors = 0
        self.start = time.monotonic()

    def record(self, ms: float) -> None:
        with self._lock:
            self.ops += 1
            self.total_ms += ms

    def record_error(self) -> None:
        with self._lock:
            self.errors += 1

    def snapshot(self) -> tuple[int, float, int, float]:
        """Return (ops, avg_latency_ms, errors, elapsed_seconds)."""
        with self._lock:
            elapsed = max(time.monotonic() - self.start, 1e-9)
            avg = (self.total_ms / self.ops) if self.ops else 0.0
            return self.ops, avg, self.errors, elapsed
