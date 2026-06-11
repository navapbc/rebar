"""CLI-interface-specific behaviors (the `rebar` console script).

Covers argv routing, the exit-10 passthrough contract, cwd/REBAR_ROOT
resolution, and reconcile interception — behaviors that the library/MCP tiers
don't exercise.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import rebar


def _cli(
    *args: str, cwd: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True, text=True, cwd=cwd, env=env,
    )


@pytest.fixture
def offline_acli_env(tmp_path: Path) -> dict[str, str]:
    """A subprocess env whose ``acli`` is a fake returning an empty issue list,
    with no JIRA_* creds — so a reconcile pass runs fully offline (fetch -> [] ->
    no-write report) instead of reaching real Jira. Keeps the default interface
    tier hermetic and fast (~1s vs ~45s against live Jira)."""
    bindir = tmp_path / "offline-bin"
    bindir.mkdir()
    acli = bindir / "acli"
    acli.write_text("#!/bin/sh\necho '[]'\nexit 0\n")
    acli.chmod(acli.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    for var in ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN"):
        env.pop(var, None)
    return env


def test_exit_10_passthrough(rebar_repo: Path) -> None:
    """A rejected transition (valid-but-stale status) must surface the engine's
    exit code 10 through the CLI, unchanged (not collapsed to 1)."""
    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    cp = _cli("transition", tid, "in_progress", "closed")
    assert cp.returncode == 10
    # Store unchanged.
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open"


def test_cwd_independent_resolution(rebar_repo: Path, tmp_path: Path) -> None:
    """Invoked from an unrelated cwd, the CLI still targets REBAR_ROOT."""
    tid = rebar.create_ticket("task", "From elsewhere", repo_root=str(rebar_repo))
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    cp = _cli("show", tid, cwd=str(elsewhere))
    assert cp.returncode == 0
    assert "From elsewhere" in cp.stdout


def test_unknown_subcommand_errors(rebar_repo: Path) -> None:
    cp = _cli("definitely-not-a-command")
    assert cp.returncode != 0
    assert "unknown subcommand" in (cp.stdout + cp.stderr).lower()


def test_help_flag_shows_usage_without_executing(rebar_repo: Path) -> None:
    """`rebar <sub> --help` prints the subcommand usage and exits 0 WITHOUT
    executing the command. `init` is the canonical example: it has no required
    args, so it used to run (and re-initialize) instead of showing help."""
    cp = _cli("init", "--help", cwd=str(rebar_repo))
    assert cp.returncode == 0
    assert "Usage: rebar init" in cp.stdout
    # init prints "initialized" when it actually runs — must be absent here.
    assert "initialized" not in (cp.stdout + cp.stderr).lower()


def test_subcommand_help_has_no_side_effect(rebar_repo: Path) -> None:
    """`rebar create --help` must show usage and create nothing."""
    before = len(rebar.list_tickets(repo_root=str(rebar_repo)))
    cp = _cli("create", "--help", cwd=str(rebar_repo))
    assert cp.returncode == 0
    assert "Usage: rebar create" in cp.stdout
    after = len(rebar.list_tickets(repo_root=str(rebar_repo)))
    assert after == before  # no ticket was created


def test_top_level_help_forms_list_subcommands(rebar_repo: Path) -> None:
    for form in ("--help", "-h", "help"):
        cp = _cli(form, cwd=str(rebar_repo))
        assert cp.returncode == 0, form
        assert "Subcommands:" in cp.stdout, form


def test_help_word_with_subcommand(rebar_repo: Path) -> None:
    cp = _cli("help", "transition", cwd=str(rebar_repo))
    assert cp.returncode == 0
    assert "Usage: rebar transition" in cp.stdout


def test_short_help_flag_on_subcommand(rebar_repo: Path) -> None:
    cp = _cli("tag", "-h", cwd=str(rebar_repo))
    assert cp.returncode == 0
    assert "Usage: rebar tag" in cp.stdout


def test_help_in_freetext_title_is_not_intercepted(rebar_repo: Path) -> None:
    """`--help` appearing in a free-text parameter (the title is at position 2,
    after the type) must NOT trigger help — the ticket is created normally and
    the literal text is preserved."""
    before = len(rebar.list_tickets(repo_root=str(rebar_repo)))
    cp = _cli("create", "task", "please pass --help to the parser", cwd=str(rebar_repo))
    assert cp.returncode == 0
    assert "Usage: rebar create" not in cp.stdout  # not the help text
    after = rebar.list_tickets(repo_root=str(rebar_repo))
    assert len(after) == before + 1
    assert any("--help" in t["title"] for t in after)


def test_bare_help_word_in_freetext_is_not_intercepted(rebar_repo: Path) -> None:
    """The bare word `help` is honored only at top level; as a free-text value
    (a title here) it is stored literally, not treated as a help request."""
    tid = rebar.create_ticket("task", "placeholder", repo_root=str(rebar_repo))
    cp = _cli("comment", tid, "run with --help or -h for usage", cwd=str(rebar_repo))
    assert cp.returncode == 0
    assert "Usage:" not in cp.stdout  # the comment was added, not help shown
    comments = rebar.show_ticket(tid, repo_root=str(rebar_repo))["comments"]
    assert any("--help" in c["body"] for c in comments)


def test_reconcile_intercepted_dry_run_default(
    rebar_repo: Path, offline_acli_env: dict[str, str]
) -> None:
    """`rebar reconcile` is intercepted and routed to the reconciler in dry-run
    by default — it must not crash the CLI with an unknown-subcommand error (the
    dispatcher has no reconcile arm).

    Hermetic: a fake empty ``acli`` (no JIRA creds) lets the dry-run pass run
    fully offline. The live-Jira behaviour is covered by the opt-in integration
    test below, so this routing check never touches the network."""
    offline_acli_env["REBAR_ROOT"] = str(rebar_repo)
    offline_acli_env["PROJECT_ROOT"] = str(rebar_repo)
    cp = _cli("reconcile", cwd=str(rebar_repo), env=offline_acli_env)
    assert "unknown subcommand 'reconcile'" not in (cp.stdout + cp.stderr).lower()


@pytest.mark.integration
def test_reconcile_dry_run_against_live_jira(rebar_repo: Path) -> None:
    """External integration check: `rebar reconcile` runs a real dry-run against
    the configured live Jira. Excluded from the default run (``-m "not
    integration"``); run it when reconcile/Jira behaviour is impacted, in an
    environment with `acli` + credentials. Asserts the pass is routed and stays
    non-mutating (dry-run, no writes)."""
    if shutil.which("acli") is None:
        pytest.skip("requires `acli` on PATH + live Jira credentials")
    cp = _cli("reconcile", cwd=str(rebar_repo))
    out = cp.stdout + cp.stderr
    assert "unknown subcommand 'reconcile'" not in out.lower()
    # A dry-run must never write — surfaced in the pass report.
    assert '"mode": "dry-run"' in out or '"no_write": true' in out or cp.returncode == 0


def test_validate_is_repo_wide_no_ticket_id(rebar_repo: Path) -> None:
    """`validate` is repo-wide: it takes NO ticket id. Passing one must not be
    accepted as a positional (regression: the engine rejected positionals with
    'Unknown option', breaking the lib/MCP surface that wrongly passed a id).

    The library `rebar.validate()` takes no ticket id, tolerates the
    score-encoded nonzero exit, and returns the parsed JSON report (not an
    error dict)."""
    # CLI: no-id invocation produces a real JSON report, not an "Unknown option".
    cp = _cli("validate", "--output", "json", cwd=str(rebar_repo))
    assert "unknown option" not in (cp.stdout + cp.stderr).lower()
    import json
    report = json.loads(cp.stdout)
    assert "score" in report

    # Library: same report, never an {"output": "Unknown option..."} error dict.
    r = rebar.validate(repo_root=str(rebar_repo))
    assert isinstance(r, dict)
    assert "score" in r
    for key in ("critical_issues", "major_issues", "minor_issues",
                "warnings", "suggestions"):
        assert key in r
