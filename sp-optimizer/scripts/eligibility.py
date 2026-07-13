"""Workload eligibility + value-validity classification (procedure-agnostic).

The optimizer only draws a conclusion about a procedure's *plan* when it has
actually captured a valid plan for a **representative** call. A great many things
can make a generated call unrepresentative or impossible without it being a
performance problem at all:

  * the synthesized value doesn't fit the parameter's declared SQL type
    (``1000`` into a ``tinyint`` → conversion error, not a bad plan);
  * two independently-real values form an invalid call together (a role name and
    a user name that happen to collide on a special principal);
  * the parameter carries a secret (a password) that must never be mined from
    data or written to an artifact;
  * the parameter is a table-valued type or a structured JSON/XML payload that a
    scalar literal cannot represent;
  * the server is missing a component the proc needs (Full-Text Search);
  * the proc requires predecessor state (a paired setup/teardown), or is an
    unbounded bulk generator that must not be run blind.

This module is the single, generic place those conditions are recognized. It has
**no procedure-specific constants** — everything keys off declared types, column
metadata, parameter-name shape, and the proc body — so it stays valid for any
procedure. Nothing here touches the database directly; callers pass in the text
and metadata they already read during discovery, which keeps every function pure
and unit-testable.

The vocabulary of non-plan outcomes (used across discover / capture / analyze /
session) is:

    ok                        a normal, scorable call
    invalid_input             a value doesn't fit its declared type/domain
    requires_curated_workload safe values cannot be inferred (TVP, JSON, cross-
                              parameter/business validity, special principals)
    requires_sensitive_input  a secret is needed and must be caller-supplied
    requires_setup            predecessor/lifecycle state is required
    blocked_prerequisite      a server component/feature is missing
    capture_failed            capture produced no analyzable plan
    timeout / cancelled       the call exceeded its cost/time budget
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

# ---- status vocabulary ------------------------------------------------------

OK = "ok"
INVALID_INPUT = "invalid_input"
REQUIRES_CURATED_WORKLOAD = "requires_curated_workload"
REQUIRES_SENSITIVE_INPUT = "requires_sensitive_input"
REQUIRES_SETUP = "requires_setup"
BLOCKED_PREREQUISITE = "blocked_prerequisite"
CAPTURE_FAILED = "capture_failed"
NOT_ANALYZABLE = "not_analyzable"
TIMEOUT = "timeout"
CANCELLED = "cancelled"

# Statuses that mean "this call was NOT a scorable plan": they must never be fed
# into the aggregate plan score, the good-fraction, or a decision to `apply`.
NON_SCORABLE = frozenset({
    INVALID_INPUT,
    REQUIRES_CURATED_WORKLOAD,
    REQUIRES_SENSITIVE_INPUT,
    REQUIRES_SETUP,
    BLOCKED_PREREQUISITE,
    CAPTURE_FAILED,
    NOT_ANALYZABLE,
    TIMEOUT,
    CANCELLED,
})


# ---- 1. type-aware numeric ranges (Issue 5) --------------------------------

# Inclusive (min, max) domains for the exact integer types. tinyint is the one
# that bites most often — it is UNSIGNED 0..255 in SQL Server, so the generic
# "big integer" synth constants (1000, 999999) overflow it.
_INT_RANGES: dict[str, tuple[int, int]] = {
    "tinyint": (0, 255),
    "smallint": (-32768, 32767),
    "int": (-2147483648, 2147483647),
    "bigint": (-9223372036854775808, 9223372036854775807),
}

_INT_TYPES = tuple(_INT_RANGES)


def _base_type(sql_type: str) -> str:
    """Strip the ``(...)`` size/precision suffix and lowercase: ``int`` from
    ``int``, ``decimal`` from ``decimal(9,2)``, ``nvarchar`` from
    ``nvarchar(2000)``."""
    return re.split(r"[\s(]", (sql_type or "").strip().lower(), 1)[0]


def _precision_scale(sql_type: str) -> tuple[Optional[int], Optional[int]]:
    """Return (precision, scale) parsed from ``decimal(p,s)`` / ``numeric(p)``."""
    m = re.search(r"\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)", sql_type or "")
    if not m:
        return None, None
    precision = int(m.group(1))
    scale = int(m.group(2)) if m.group(2) is not None else 0
    return precision, scale


def numeric_bounds(sql_type: str) -> Optional[tuple]:
    """Return the inclusive (min, max) domain for a numeric SQL type, or None if
    the type isn't a bounded numeric.

    Handles the exact integer types (tinyint/smallint/int/bigint and the ``bit``
    boolean) directly, and derives the bound for ``decimal``/``numeric`` from its
    declared precision/scale (``decimal(5,2)`` → ±999.99)."""
    base = _base_type(sql_type)
    if base == "bit":
        return (0, 1)
    if base in _INT_RANGES:
        return _INT_RANGES[base]
    if base in ("decimal", "numeric"):
        precision, scale = _precision_scale(sql_type)
        if precision is None:
            precision, scale = 18, 0  # SQL Server default
        limit = (Decimal(10) ** (precision - (scale or 0))) - (Decimal(10) ** -(scale or 0))
        return (-limit, limit)
    return None


def numeric_synth_values(sql_type: str) -> Optional[list]:
    """Type-aware boundary + typical candidates for a numeric type, guaranteed to
    fit its declared range. Returns None for non-numeric types."""
    bounds = numeric_bounds(sql_type)
    if bounds is None:
        return None
    lo, hi = bounds
    base = _base_type(sql_type)
    if base == "bit":
        return [0, 1]
    if base in _INT_RANGES:
        # low / typical / a value near the top of the *declared* range — never a
        # constant that overflows the type.
        typical = 1 if hi >= 1 else hi
        candidates = [lo if lo >= 0 else 0, typical, min(hi, 1000), hi]
        # de-dup while preserving order
        seen: set = set()
        out = []
        for v in candidates:
            if v not in seen:
                seen.add(v)
                out.append(v)
        return out
    # decimal / numeric
    _p, scale = _precision_scale(sql_type)
    step = Decimal(1).scaleb(-(scale or 0)) if scale else Decimal(1)
    typical = Decimal("1.5") if (scale or 0) >= 1 and hi >= Decimal("1.5") else min(Decimal(1), hi)
    return [Decimal(0), typical, (hi - step)]


def value_fits_type(value, sql_type: str) -> bool:
    """True if ``value`` can be sent for a parameter of ``sql_type`` without a
    conversion/overflow/length error. NULL always fits (nullability is a separate
    per-call concern). Non-constrained types default to True."""
    if value is None:
        return True
    base = _base_type(sql_type)

    if base == "bit":
        return value in (0, 1, True, False)

    if base in _INT_RANGES:
        try:
            iv = int(value)
        except (TypeError, ValueError):
            return False
        # a non-integral float (1.5) does not fit an integer column
        if isinstance(value, float) and not float(value).is_integer():
            return False
        lo, hi = _INT_RANGES[base]
        return lo <= iv <= hi

    if base in ("decimal", "numeric", "money", "smallmoney", "float", "real"):
        try:
            dv = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return False
        bounds = numeric_bounds(sql_type)
        if bounds is not None:
            lo, hi = bounds
            if not (Decimal(lo) <= dv <= Decimal(hi)):
                return False
        return True

    if base in ("varchar", "char", "nvarchar", "nchar"):
        length = _length(sql_type)
        if length is not None and length >= 0 and isinstance(value, str):
            return len(value) <= length
        return True

    return True


def _length(sql_type: str) -> Optional[int]:
    """Declared character length, or None for ``(max)`` / unspecified."""
    m = re.search(r"\(\s*(\d+|max)\s*\)", sql_type or "", re.IGNORECASE)
    if not m:
        return None
    tok = m.group(1).lower()
    return None if tok == "max" else int(tok)


# ---- 2. sensitive-parameter detection (Issue 10) ---------------------------

# Name fragments that mark a parameter as carrying secret material. Matched on
# the de-sigiled, lowercased name. Kept generic (no proc-specific names) so it
# recognizes @NewPassword, @OldPassword, @ApiToken, @SecretKey, ... alike.
_SENSITIVE_FRAGMENTS = (
    "password", "passwd", "pwd", "secret", "token", "apikey", "api_key",
    "credential", "privatekey", "private_key", "passphrase", "otp", "pin",
    "sessionkey", "session_key", "accesskey", "access_key", "authtoken",
)

# Fragments that look sensitive but are benign (a hash-check flag, an id, etc.)
# would go here; kept empty for now — precision beats a false negative on secrets.


def is_sensitive_param(param_name: str) -> bool:
    """True when a parameter name indicates it holds a secret (password, token,
    key, ...). Secrets are never mined from data, never persisted verbatim, and
    require a caller-supplied value for a valid call."""
    name = (param_name or "").lstrip("@").lower()
    return any(frag in name for frag in _SENSITIVE_FRAGMENTS)


def redact(value) -> str:
    """Render a sensitive value as a fixed redaction token — never the value."""
    return "***REDACTED***"


# ---- 3. structured scalar formats: JSON / XML (Issue 12) -------------------

_JSON_CUES = ("isjson", "openjson", "json_value", "json_query", "json_modify")


def param_expects_json(proc_text: str, param_name: str) -> bool:
    """True when the proc validates/parses ``param_name`` as JSON (ISJSON/OPENJSON
    over it, or a JSON_* function reading it)."""
    text = proc_text or ""
    esc = re.escape(param_name)
    # Direct: OPENJSON(@p) / ISJSON(@p) / JSON_VALUE(@p, ...)
    for fn in _JSON_CUES:
        if re.search(fn + r"\s*\(\s*" + esc + r"\b", text, re.IGNORECASE):
            return True
    # Indirect: any JSON cue in a proc whose only string param is this one is a
    # strong hint; keep it conservative and require the param to appear in a JSON
    # function call, so we don't over-trigger.
    return False


def param_expects_xml(proc_text: str, param_name: str, sql_type: str) -> bool:
    """True when the parameter is XML-typed or parsed with XML methods."""
    if _base_type(sql_type) == "xml":
        return True
    text = proc_text or ""
    esc = re.escape(param_name)
    return bool(re.search(esc + r"\s*\.\s*(?:value|query|nodes|exist)\s*\(",
                          text, re.IGNORECASE))


# ---- 4. Full-Text Search prerequisite (Issue 4) ----------------------------

_FULLTEXT_PREDICATES = ("containstable", "freetexttable", "contains", "freetext")


def requires_full_text(proc_text: str) -> bool:
    """True when the proc uses a Full-Text predicate (CONTAINS/FREETEXT/…), which
    needs the Full-Text Search component installed on the server."""
    text = proc_text or ""
    for kw in _FULLTEXT_PREDICATES:
        if re.search(r"\b" + kw + r"\s*\(", text, re.IGNORECASE):
            return True
    return False


# ---- 5. unbounded / bulk-generator detection (Issue 7) ---------------------

_CURRENT_DATE_FNS = ("getdate", "sysdatetime", "getutcdate", "sysutcdatetime", "current_timestamp")


def is_bulk_generator(proc_text: str, proc_name: str = "") -> Optional[str]:
    """Return a human reason string when the proc looks like an unbounded bulk
    data generator / storage rebuild (loops toward the current date, mass-inserts,
    or is named as a data-load routine), else None.

    Recognized generically:
      * a WHILE loop whose body advances toward GETDATE()/SYSDATETIME() (a
        "populate to current date" loop),
      * repeated INSERT ... SELECT inside a loop (bulk generation),
      * name/schema hints (DataLoad*, *Populate*, *Generate*, *RebuildStorage*).
    """
    text = proc_text or ""
    low = text.lower()
    name = (proc_name or "").lower()

    has_while = bool(re.search(r"\bwhile\b", low))
    hits_current_date = any(fn + "(" in low for fn in _CURRENT_DATE_FNS)
    if has_while and hits_current_date:
        return "loops toward the current date (unbounded data generation)"

    if has_while and re.search(r"\binsert\b", low):
        return "inserts inside a loop (potentially unbounded bulk generation)"

    for hint in ("dataload", "populatedata", "populate", "generatedata", "rebuildstorage"):
        if hint in name:
            return f"name indicates a bulk data-load / generation routine ('{hint}')"

    return None


# ---- 6. paired setup/teardown lifecycle (Issue 9) --------------------------

# Antonym stems that mark a proc as one half of a setup/teardown pair. The
# "action" group names WHAT the second half undoes (reactivate → deactivate);
# the "position" group names WHEN it runs relative to the work (after → before).
# A teardown-side proc typically carries one of each, so we swap at most one stem
# from each group. Ordered longest-first within a group so "reactivate" wins over
# its own "activate" substring. No hard-coded proc name anywhere.
_ACTION_PAIRS = [
    ("reactivate", "deactivate"),
    ("activate", "deactivate"),
    ("reenable", "disable"),
    ("enable", "disable"),
    ("resume", "suspend"),
    ("teardown", "setup"),
    ("cleanup", "setup"),
]
_POSITION_PAIRS = [
    ("after", "before"),
    ("post", "pre"),
]


_TOKEN_RE = re.compile(r"[A-Z][a-z0-9]*|[a-z0-9]+")


def _swap_first(text: str, pairs) -> tuple[str, bool]:
    """Swap the first antonym stem that appears as a WHOLE CamelCase token in
    ``text`` (case-preserving). Token-based matching avoids substring false
    positives — e.g. the ``resume`` stem must not fire inside ``Resumes``."""
    lookup = {a: b for a, b in pairs}
    tokens = list(_TOKEN_RE.finditer(text))
    for m in tokens:
        tok = m.group(0)
        repl = lookup.get(tok.lower())
        if repl is not None:
            return text[:m.start()] + _match_case(tok, repl) + text[m.end():], True
    return text, False


def setup_partner(proc_name: str) -> Optional[str]:
    """If ``proc_name`` is the second half of a setup/teardown pair, return the
    inferred name of the first half (its required predecessor), else None.

    e.g. ``DataLoadSimulation.ReactivateTemporalTablesAfterDataLoad`` →
    ``DataLoadSimulation.DeactivateTemporalTablesBeforeDataLoad``. Purely
    lexical: it swaps the antonym stems, so it never hard-codes a proc name."""
    if not proc_name:
        return None
    schema, _, short = proc_name.rpartition(".")
    partner, hit_action = _swap_first(short, _ACTION_PAIRS)
    partner, hit_pos = _swap_first(partner, _POSITION_PAIRS)
    if not (hit_action or hit_pos):
        return None
    return f"{schema}.{partner}" if schema else partner


def _match_case(sample: str, replacement: str) -> str:
    """Render ``replacement`` in the capitalization style of ``sample``."""
    if sample.isupper():
        return replacement.upper()
    if sample[:1].isupper():
        return replacement.capitalize()
    return replacement


# ---- 7. cross-parameter / special-principal validity (Issue 6) -------------

# Fixed server/database principals and reserved names that cannot be passed where
# an ordinary user/role is expected (SQL Server error 15405 family). Lower-cased.
_SPECIAL_PRINCIPALS = {
    "sys", "information_schema", "guest", "public",
}
_FIXED_ROLE_PREFIXES = ("db_", "sql_")


def is_special_principal(value) -> bool:
    """True when a value names a fixed/reserved database principal that cannot be
    used as a target of role/user administration (e.g. ``db_backupoperator``,
    ``sys``, ``public``)."""
    if not isinstance(value, str):
        return False
    v = value.strip().lower()
    if v in _SPECIAL_PRINCIPALS:
        return True
    return v.startswith(_FIXED_ROLE_PREFIXES)


def _looks_like(name: str, *fragments: str) -> bool:
    n = (name or "").lstrip("@").lower()
    return any(f in n for f in fragments)


def validate_principal_combo(values: dict) -> Optional[str]:
    """Cross-parameter check for administrative role/user procs.

    Returns a reason string when the *combination* is invalid even though each
    value may be independently real:
      * a role-name param and a user/member-name param resolve to the SAME value
        (you can't add a principal to itself), or
      * a user/member param is bound to a special/fixed principal that SQL Server
        forbids as a target (error 15405).
    Returns None when no principal-shaped parameters are present or the combo is
    fine. Generic: it keys off parameter-name shape, not specific proc names.
    """
    role_params = {k: v for k, v in values.items() if _looks_like(k, "role")}
    user_params = {
        k: v for k, v in values.items()
        if _looks_like(k, "user", "member", "login", "principal") and k not in role_params
    }
    if not role_params and not user_params:
        return None

    # Same concrete value used for a role AND a user parameter.
    role_vals = {str(v).lower() for v in role_params.values() if v is not None}
    user_vals = {str(v).lower() for v in user_params.values() if v is not None}
    collision = role_vals & user_vals
    if collision:
        return (f"role and user parameters share the same principal "
                f"{sorted(collision)!r}; a principal cannot be administered onto itself")

    # A user/member target bound to a special principal.
    for k, v in user_params.items():
        if is_special_principal(v):
            return (f"{k} is a special/fixed principal ({v!r}) that SQL Server "
                    f"forbids as an administration target (error 15405)")
    return None


# ---- 8. SQL error classification (Issues 3,4,5,6,9,11,12) -------------------

# Each entry: (compiled matcher, status, human reason). First match wins. The
# matchers key off SQL Server's stable error numbers where possible and the
# message text otherwise, so a capture failure is classified as the real cause
# (missing feature, invalid input, needed setup) instead of a zero plan score.
_ERROR_RULES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\b7609\b|full[- ]?text search is not installed|full-text component",
                re.IGNORECASE),
     BLOCKED_PREREQUISITE, "Full-Text Search is not installed on this server (SQL error 7609)"),
    (re.compile(r"\b8114\b|error converting data type", re.IGNORECASE),
     INVALID_INPUT, "a generated value could not convert to the parameter's declared type (SQL error 8114)"),
    (re.compile(r"\b15405\b|cannot use the special principal", re.IGNORECASE),
     REQUIRES_CURATED_WORKLOAD, "the call used a special/fixed principal (SQL error 15405)"),
    (re.compile(r"\b13597\b|system_time period is already defined", re.IGNORECASE),
     REQUIRES_SETUP, "the proc requires predecessor temporal/lifecycle state (SQL error 13597)"),
    (re.compile(r"operand type clash.*incompatible with", re.IGNORECASE | re.DOTALL),
     REQUIRES_CURATED_WORKLOAD, "a table-valued/typed parameter cannot be represented as a scalar literal (SQL error 206)"),
    (re.compile(r"query timeout expired|\bHYT00\b|operation (?:cancelled|canceled)|\btimeout\b",
                re.IGNORECASE),
     TIMEOUT, "the call exceeded its command timeout and was cancelled"),
]


def classify_sql_error(error: Optional[str]) -> Optional[tuple[str, str]]:
    """Map a captured SQL error string to (status, reason) when it is a known
    non-plan condition (missing feature, invalid input, needed setup, timeout,
    special principal, TVP clash). Returns None for an unrecognized error, which
    the caller then treats as an ordinary capture failure."""
    if not error:
        return None
    for pattern, status, reason in _ERROR_RULES:
        if pattern.search(error):
            return status, reason
    return None
