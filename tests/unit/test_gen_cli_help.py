"""Happy-path oracle for the canonical CLI help generator (RP-05 S2d).

These tests pin the *observable contract* of ``scripts/gen_cli_help.py`` and the
regenerated ``rebar/_cli/help`` artifacts — exit codes, determinism, the capitalized
``Usage:`` prefix invariant, and overview uniqueness/non-blankness — without asserting
any private structure of the generator. Edge/E2E parity is withheld (held-out oracle).
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN = REPO_ROOT / "scripts" / "gen_cli_help.py"
HELP_DIR = REPO_ROOT / "src" / "rebar" / "_cli" / "help"
IMPORT_GUIDE = REPO_ROOT / "docs" / "import-export.md"


def _load_gen():
    spec = importlib.util.spec_from_file_location("gen_cli_help", GEN)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_gen(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GEN), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )


def test_check_mode_passes_on_committed_artifacts() -> None:
    """The committed artifacts are exactly what the generator would write."""
    cp = _run_gen("--check")
    assert cp.returncode == 0, f"--check failed (stale artifacts?):\n{cp.stdout}\n{cp.stderr}"


def test_public_write_generates_every_visible_intercept_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    """The public generator writes one artifact for each visible intercept route."""
    from rebar._cli._registry import ROUTES

    mod = _load_gen()
    sandbox = tmp_path / "help"
    monkeypatch.setattr(mod, "HELP_DIR", sandbox)

    assert mod.main([]) == 0

    required = {
        f"{route.name}.txt"
        for route in ROUTES
        if route.group == "intercept" and not route.hidden and not route.retired
    }
    written = {path.name for path in sandbox.glob("*.txt")}
    assert required
    assert required <= written


def _write_visible_intercepts(tmp_path: Path, monkeypatch, *, columns: int) -> dict[str, bytes]:
    from rebar._cli._registry import ROUTES

    monkeypatch.setenv("COLUMNS", str(columns))
    mod = _load_gen()
    destination = tmp_path / f"help-{columns}"
    monkeypatch.setattr(mod, "HELP_DIR", destination)
    assert mod.main([]) == 0

    names = {
        f"{route.name}.txt"
        for route in ROUTES
        if route.group == "intercept" and not route.hidden and not route.retired
    }
    assert names
    assert names <= {path.name for path in destination.glob("*.txt")}
    return {name: (destination / name).read_bytes() for name in names}


def test_visible_intercept_artifacts_ignore_terminal_width(tmp_path: Path, monkeypatch) -> None:
    """Intercept artifact bytes do not depend on the terminal width."""
    narrow = _write_visible_intercepts(tmp_path, monkeypatch, columns=50)
    wide = _write_visible_intercepts(tmp_path, monkeypatch, columns=140)

    assert narrow == wide


def test_trusted_env_preserves_explicit_multiline_usage(tmp_path: Path, monkeypatch) -> None:
    """The trusted-env artifact keeps its explicit add and revoke usage lines."""
    artifacts = _write_visible_intercepts(tmp_path, monkeypatch, columns=80)

    lines = artifacts["trusted-env.txt"].decode("utf-8").splitlines()
    assert lines[:2] == [
        "Usage: rebar trusted-env add <env_id> <public_key> [--root <path>]",
        "rebar trusted-env revoke <env_id> <public_key-or-index> [--root <path>]",
    ]


def test_import_help_and_user_guide_describe_current_recovery_contract() -> None:
    """The generated import help and user guide state the shipped import contract."""
    help_text = " ".join((HELP_DIR / "import.txt").read_text(encoding="utf-8").split())
    guide = IMPORT_GUIDE.read_text(encoding="utf-8")

    assert "batches of up to 256 events" in help_text
    assert "Links and status changes are committed one event at a time." in help_text
    assert "defers the push until import work finishes successfully" in help_text
    assert "chunks of up to 256 events" in guide
    assert "Link and status passes remain one event per commit" in guide
    assert "pushes once after import work finishes successfully" in guide

    for text in (help_text, guide):
        assert "does not provide whole-file atomicity" in text
        assert "serial rerun" in text
        assert "does not complete the missing events automatically" in text
        assert "Concurrent imports" in text

    assert "there is currently no batch-commit primitive" not in guide
    assert "one git commit + one lock cycle per event" not in guide


def test_write_mode_is_idempotent_and_check_clean(tmp_path, monkeypatch) -> None:
    """Writing renders deterministic bytes and a subsequent --check is clean.

    Runs against an ISOLATED copy of the help dir (never the tracked tree) so a parallel
    worker can never observe a transient mutation of ``src/rebar/_cli/help`` — the tracked
    artifacts are only ever READ (see ``test_check_mode_passes_on_committed_artifacts``)."""
    mod = _load_gen()
    sandbox = tmp_path / "help"
    shutil.copytree(HELP_DIR, sandbox)
    monkeypatch.setattr(mod, "HELP_DIR", sandbox)

    assert mod._write() == 0
    first = {p.name: p.read_bytes() for p in sandbox.glob("*.txt")}
    assert mod._check() == 0, "a write then --check must be clean"
    assert mod._write() == 0
    second = {p.name: p.read_bytes() for p in sandbox.glob("*.txt")}
    assert first == second, "generator write is non-deterministic"


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


def test_check_detects_a_stale_artifact(tmp_path, monkeypatch) -> None:
    """--check has teeth: a mutated artifact makes it fail.

    Mutates an ISOLATED copy (never the tracked ``init.txt``) so the transient dirty state
    cannot leak to a parallel worker's working-tree-cleanliness guard (bug: an in-place
    mutate/restore here raced the store-isolation harness)."""
    mod = _load_gen()
    sandbox = tmp_path / "help"
    shutil.copytree(HELP_DIR, sandbox)
    monkeypatch.setattr(mod, "HELP_DIR", sandbox)
    assert mod._check() == 0

    victim = sandbox / "init.txt"
    victim.write_bytes(victim.read_bytes() + b"\nstray line\n")
    assert mod._check() != 0, "--check did not detect a stale artifact"


def test_check_detects_a_missing_artifact(tmp_path, monkeypatch) -> None:
    """--check fails when a required artifact is absent (a route with no rendered .txt).

    Isolated copy only — never the tracked tree."""
    mod = _load_gen()
    sandbox = tmp_path / "help"
    shutil.copytree(HELP_DIR, sandbox)
    monkeypatch.setattr(mod, "HELP_DIR", sandbox)
    assert mod._check() == 0

    (sandbox / "init.txt").unlink()
    assert mod._check() != 0, "--check did not detect a missing artifact"


def test_check_detects_a_stray_artifact_and_write_prunes_it(tmp_path, monkeypatch) -> None:
    """--check fails on an unexpected artifact, and a subsequent write prunes it clean.

    Isolated copy only — never the tracked tree."""
    mod = _load_gen()
    sandbox = tmp_path / "help"
    shutil.copytree(HELP_DIR, sandbox)
    monkeypatch.setattr(mod, "HELP_DIR", sandbox)
    assert mod._check() == 0

    stray = sandbox / "not-a-real-command.txt"
    stray.write_bytes(b"stray\n")
    assert mod._check() != 0, "--check did not detect a stray artifact"

    assert mod._write() == 0
    assert not stray.exists(), "write did not prune the stray artifact"
    assert mod._check() == 0, "--check not clean after write pruned the stray artifact"


def test_check_hard_fails_on_a_blank_overview_summary(monkeypatch, capsys) -> None:
    """The blank-summary guard has teeth: if a VISIBLE command's parser summary is blank,
    --check fails hard and names the offending command (so a factory that forgot its
    ``description`` cannot ship a blank overview one-liner)."""
    mod = _load_gen()
    real = mod._summary
    monkeypatch.setattr(mod, "_summary", lambda r: "" if r.name == "show" else real(r))

    rc = mod._check()

    assert rc == 1
    err = capsys.readouterr().err
    assert "blank overview" in err and "show" in err, err
