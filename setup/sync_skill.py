#!/usr/bin/env python3
"""Cross-platform repository-local skill discovery: keep the checked-in
``.claude/skills/sp-optimizer/SKILL.md`` byte-identical to the canonical
``sp-optimizer/SKILL.md``, and prove it.

Why this exists
---------------
Claude Code discovers a repo's skills from ``.claude/skills/<name>/SKILL.md``.
That entry used to be a git **symlink** into ``sp-optimizer/``. Symlinks are not
reliably materialized on a Windows checkout (without Developer Mode /
``core.symlinks=true`` git leaves an 18-byte *text file* containing the link
target), so natural-language invocation could fail repo-local discovery or fall
back to some other skill source in the runtime. The result was non-deterministic:
the checked-out repository was not guaranteed to be what actually ran.

The deterministic fix is a **real, committed file** at
``.claude/skills/sp-optimizer/SKILL.md`` — no symlink semantics, identical on
Windows and Unix — kept in sync with the canonical copy by this script. All paths
are resolved relative to this file, so nothing depends on a machine-specific
absolute path.

Usage (run from anywhere; paths are resolved relative to the repo)::

    python setup/sync_skill.py verify   # exit 0 iff the two SKILL.md match (hash)
    python setup/sync_skill.py sync     # copy canonical -> .claude (explicit action)
    python setup/sync_skill.py path     # print both resolved paths + hashes

``sync`` only ever writes inside THIS repository's ``.claude/skills`` (project
scope). It never touches a globally installed skill (``~/.claude/skills``); a
global install/update remains an explicit, separate action the user takes.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL = REPO_ROOT / "sp-optimizer" / "SKILL.md"
DISCOVERED = REPO_ROOT / ".claude" / "skills" / "sp-optimizer" / "SKILL.md"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cmd_path() -> int:
    for label, p in (("canonical", CANONICAL), ("discovered", DISCOVERED)):
        if p.exists():
            print(f"{label:11s} {p}  sha256={_sha256(p)}")
        else:
            print(f"{label:11s} {p}  (MISSING)")
    return 0


def cmd_verify() -> int:
    if not CANONICAL.exists():
        print(f"FAIL: canonical skill missing: {CANONICAL}", file=sys.stderr)
        return 2
    if not DISCOVERED.exists():
        print(f"FAIL: repo-local skill missing: {DISCOVERED}\n"
              f"      run: python setup/sync_skill.py sync", file=sys.stderr)
        return 1
    a, b = _sha256(CANONICAL), _sha256(DISCOVERED)
    if a == b:
        print(f"OK: repo-local skill matches canonical (sha256={a})")
        print(f"    discovered: {DISCOVERED}")
        return 0
    print(f"FAIL: repo-local skill has drifted from canonical\n"
          f"      canonical  {CANONICAL}  sha256={a}\n"
          f"      discovered {DISCOVERED}  sha256={b}\n"
          f"      run: python setup/sync_skill.py sync", file=sys.stderr)
    return 1


def cmd_sync() -> int:
    if not CANONICAL.exists():
        print(f"FAIL: canonical skill missing: {CANONICAL}", file=sys.stderr)
        return 2
    DISCOVERED.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CANONICAL, DISCOVERED)
    print(f"synced: {CANONICAL} -> {DISCOVERED} (sha256={_sha256(DISCOVERED)})")
    return 0


_COMMANDS = {"verify": cmd_verify, "sync": cmd_sync, "path": cmd_path}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "verify"
    fn = _COMMANDS.get(cmd)
    if fn is None:
        print(f"usage: python setup/sync_skill.py [{'|'.join(_COMMANDS)}]", file=sys.stderr)
        return 2
    return fn()


if __name__ == "__main__":
    raise SystemExit(main())
