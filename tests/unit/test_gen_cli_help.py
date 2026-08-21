"""Happy-path oracle for the canonical CLI help generator (RP-05 S2d).

These tests pin the *observable contract* of ``scripts/gen_cli_help.py`` and the
regenerated ``rebar/_cli/help`` artifacts — exit codes, determinism, the capitalized
``Usage:`` prefix invariant, and overview uniqueness/non-blankness — without asserting
any private structure of the generator. Edge/E2E parity is withheld (held-out oracle).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN = REPO_ROOT / "scripts" / "gen_cli_help.py"
HELP_DIR = REPO_ROOT / "src" / "rebar" / "_cli" / "help"


def _run_gen(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GEN), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def test_generator_script_exists() -> None:
    assert GEN.is_file(), f"{GEN} must exist"


def test_check_mode_passes_on_committed_artifacts() -> None:
    """The committed artifacts are exactly what the generator would write."""
    cp = _run_gen("--check")
    assert cp.returncode == 0, f"--check failed (stale artifacts?):\n{cp.stdout}\n{cp.stderr}"


def test_write_mode_is_idempotent_and_check_clean() -> None:
    """Writing then --check is clean, and a second write changes nothing (determinism)."""
    first = _run_gen()
    assert first.returncode == 0, f"write failed:\n{first.stdout}\n{first.stderr}"
    # git must show no diff under the help dir after a write (bytes are deterministic)
    diff = subprocess.run(
        ["git", "diff", "--quiet", "--", str(HELP_DIR)],
        cwd=str(REPO_ROOT),
    )
    assert diff.returncode == 0, "generator write produced a diff vs committed artifacts"
    assert _run_gen("--check").returncode == 0


def test_every_artifact_usage_line_is_capitalized() -> None:
    """The ONE byte-parity invariant: every artifact's usage line uses capital ``Usage:``."""
    txts = sorted(HELP_DIR.glob("*.txt"))
    assert txts, "no help artifacts found"
    for p in txts:
        first = p.read_text(encoding="utf-8").splitlines()[0]
        assert first.startswith("Usage: rebar") or p.name == "overview.txt", (
            f"{p.name}: usage line must start with capital 'Usage: rebar', got {first!r}"
        )
    overview_first = (HELP_DIR / "overview.txt").read_text(encoding="utf-8").splitlines()[0]
    assert overview_first.startswith("Usage: rebar"), overview_first


def test_overview_lists_every_visible_command_once_with_nonblank_summary() -> None:
    """Every visible canonical command appears exactly once with a non-blank one-liner."""
    overview = (HELP_DIR / "overview.txt").read_text(encoding="utf-8")
    lines = overview.splitlines()
    # subcommand rows are the indented "  <name>  <summary>" block
    rows = [ln for ln in lines if ln.startswith("  ") and ln.strip()]
    assert rows, "overview has no subcommand rows"
    seen: dict[str, int] = {}
    for ln in rows:
        name = ln.split()[0]
        seen[name] = seen.get(name, 0) + 1
        summary = ln[len(ln) - len(ln.lstrip()) :].split(None, 1)
        assert len(summary) == 2 and summary[1].strip(), (
            f"overview one-liner for {name!r} is blank: {ln!r}"
        )
    dupes = {n: c for n, c in seen.items() if c > 1}
    assert not dupes, f"overview lists commands more than once: {dupes}"


def test_check_detects_a_stale_artifact(tmp_path: Path) -> None:
    """--check has teeth: a mutated artifact makes it fail, and restoring makes it pass."""
    victim = HELP_DIR / "init.txt"
    original = victim.read_bytes()
    try:
        victim.write_bytes(original + b"\nstray line\n")
        assert _run_gen("--check").returncode != 0, "--check did not detect a stale artifact"
    finally:
        victim.write_bytes(original)
    assert _run_gen("--check").returncode == 0
