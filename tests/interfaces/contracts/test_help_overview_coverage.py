"""Drift guard: every routable rebar subcommand is advertised in the overview.

In-process re-anchor of tests/scripts/test-ticket-help-overview-coverage.sh (the
bash dispatcher it scraped is being deleted). Instead of grepping the dispatcher's
`case` arms, this checks the in-process CLI's two sources of truth and asserts they
agree:

  * ``rebar._cli`` routing — the union of the dispatch frozensets plus the
    individually-routed arms (init, scratch, delete, fsck, …). These are the
    subcommands ``main()`` will actually dispatch (i.e. NOT fall through to the
    unknown-subcommand error).
  * ``rebar._cli._help.known_subcommands()`` — the subcommands with pinned help
    text (one ``help/<sub>.txt`` each).
  * ``rebar._cli._help.overview()`` — the listed subcommands (lines ``^  <sub>``).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from rebar import _cli
from rebar._cli import _help

# Arms intentionally NOT advertised in the overview. ``help`` is the top-level help
# word (no .txt of its own). (The deprecated ``list-epics`` arm was removed in ticket
# 5899; use ``list --type=epic ...``.)
_OVERVIEW_ALLOWLIST = frozenset({"help"})
_REPO_ROOT = Path(__file__).resolve().parents[3]
_RETIRED_BRIDGE_COMMANDS = ("bridge-status", "purge-bridge")


def _routable_subcommands() -> frozenset[str]:
    """The subcommands ``_cli._dispatch`` routes (won't hit the unknown error)."""
    grouped: frozenset[str] = (
        _cli._READS_INIT_ONLY
        | _cli._READS_NO_INIT
        | _cli._FIELD_READS
        | _cli._LOOKUPS
        | _cli._DESCENDANTS
        | _cli._GATES
        | _cli._SIGNING
        | _cli._LIFECYCLE
        | _cli._COMPACT
        | _cli._BRIDGE
        | _cli._WRITES_FULL
        | _cli._IO
    )
    # Arms ``_dispatch`` routes by explicit ``if sub == …`` rather than a frozenset, plus
    # ``audit`` which is routed by a ``main()`` intercept (owns its own subcommand parsing,
    # like reconcile) yet carries pinned help text (help/audit.txt) so it is a known
    # subcommand — hence it must be counted routable here (story 46f0).
    individual = frozenset(
        {
            "init",
            "scratch",
            "metrics",
            "delete",
            "fsck",
            "fsck-recover",
            "doctor",
            "bridge-probe",
            "grounding-info",
            "audit",
        }
    )
    return grouped | individual


def _overview_listed() -> frozenset[str]:
    listed = set()
    for line in _help.overview().splitlines():
        m = re.match(r"^  ([a-z][a-z0-9-]*)( |$)", line)
        if m:
            listed.add(m.group(1))
    return frozenset(listed)


def test_routable_set_matches_pinned_help_set() -> None:
    """Every routable arm has pinned help text and vice-versa (no drift)."""
    routable = _routable_subcommands()
    known = _help.known_subcommands()
    assert routable - known == frozenset(), (
        f"routable but no pinned help text: {sorted(routable - known)}"
    )
    assert known - routable == frozenset(), (
        f"pinned help text but not routable: {sorted(known - routable)}"
    )


def test_every_known_subcommand_listed_in_overview() -> None:
    """Each known subcommand (minus the allowlist) appears in the overview."""
    known = _help.known_subcommands()
    listed = _overview_listed()
    missing = sorted((known - listed) - _OVERVIEW_ALLOWLIST)
    assert missing == [], f"subcommands missing from 'rebar help' overview: {missing}"


def test_overview_lists_no_unknown_subcommand() -> None:
    """The overview never advertises a subcommand that isn't routable."""
    listed = _overview_listed()
    known = _help.known_subcommands()
    extra = sorted(listed - known)
    assert extra == [], f"overview lists non-routable subcommands: {extra}"


@pytest.mark.parametrize("command", _RETIRED_BRIDGE_COMMANDS)
def test_retired_bridge_command_is_unknown(command: str, rebar_repo: Path) -> None:
    """A retired bridge command follows the public unknown-command contract."""
    completed = subprocess.run(
        [sys.executable, "-m", "rebar.cli", command],
        cwd=rebar_repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stderr == f"Error: unknown subcommand '{command}'\n"
    assert "Subcommands:" in completed.stdout


def test_retired_bridge_commands_are_absent_from_top_level_help() -> None:
    """Top-level help advertises neither retired bridge command."""
    completed = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "--help"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    for command in _RETIRED_BRIDGE_COMMANDS:
        assert command not in completed.stdout


def test_shipped_surfaces_name_no_retired_bridge_command() -> None:
    """The shipped package and active command-contract docs contain no stale token."""
    package_files = [
        path
        for path in (_REPO_ROOT / "src" / "rebar").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    ]
    active_docs = [
        _REPO_ROOT / "docs" / "cli-reference.md",
        _REPO_ROOT / "docs" / "exit-codes.md",
        _REPO_ROOT / "docs" / "output-schemas.md",
    ]

    matches: dict[str, list[str]] = {}
    for path in [*package_files, *active_docs]:
        text = path.read_text(encoding="utf-8")
        found = [command for command in _RETIRED_BRIDGE_COMMANDS if command in text]
        if found:
            matches[str(path.relative_to(_REPO_ROOT))] = found

    assert matches == {}
