"""Issue 1: repo-local skill discovery must be deterministic on every platform.

The discovery entry ``.claude/skills/sp-optimizer/SKILL.md`` must be a REAL file
(not a symlink that a Windows checkout would leave as an 18-byte text stub) and
must be byte-identical to the canonical ``sp-optimizer/SKILL.md``.
"""
import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CANONICAL = REPO_ROOT / "sp-optimizer" / "SKILL.md"
DISCOVERED = REPO_ROOT / ".claude" / "skills" / "sp-optimizer" / "SKILL.md"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_discovered_skill_exists_and_is_regular_file():
    assert DISCOVERED.exists(), f"missing repo-local skill: {DISCOVERED}"
    # A real file resolves cross-platform; a symlink does not on a bare Windows
    # checkout. The discovery entry must not be a symlink.
    assert not DISCOVERED.is_symlink(), "discovery entry must be a real file, not a symlink"
    assert DISCOVERED.is_file()


def test_discovered_skill_matches_canonical_hash():
    assert _sha(DISCOVERED) == _sha(CANONICAL), (
        "repo-local SKILL.md has drifted from sp-optimizer/SKILL.md — "
        "run: python setup/sync_skill.py sync")


def test_sync_verify_command_passes():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sync_skill", REPO_ROOT / "setup" / "sync_skill.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.cmd_verify() == 0
