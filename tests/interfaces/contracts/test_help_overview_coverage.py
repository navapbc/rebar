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

from rebar._cli import _help

# Arms intentionally NOT advertised in the overview. ``help`` is the top-level help
# word (no .txt of its own); bridge-fsck and bridge-probe are retained compatibility
# entrypoints whose canonical children are advertised by the bridge group.
_OVERVIEW_ALLOWLIST = frozenset({"help", "bridge-fsck", "bridge-probe"})
_REPO_ROOT = Path(__file__).resolve().parents[3]
_RETIRED_BRIDGE_COMMANDS = ("purge-bridge",)


def _routable_subcommands() -> frozenset[str]:
    """The canonical subcommands the registry routes (won't hit the unknown error).

    RP-05 S5: enumerated from the route registry via ``derive_policy_sets`` + route
    attributes rather than reconstructing the ``_cli`` policy frozensets by hand. The
    routable-with-pinned-help class is exactly the live, non-hidden, non-intercept
    routes (the intercept class owns its own ``--help`` and carries no ``help/*.txt``)."""
    from rebar._cli._registry import ROUTES

    return frozenset(
        r.name for r in ROUTES if not r.retired and not r.hidden and r.group != "intercept"
    )


def _overview_listed() -> frozenset[str]:
    listed = set()
    for line in _help.overview().splitlines():
        m = re.match(r"^  ([a-z][a-z0-9-]*)( |$)", line)
        if m:
            listed.add(m.group(1))
    return frozenset(listed)


def test_routable_set_matches_pinned_help_set() -> None:
    """Every routable arm has pinned help text and vice-versa (no drift)."""
    # ``_routable_subcommands`` already excludes hidden routes (RP-05 S6 retired the
    # router's literal ``_HIDDEN_ALIASES`` frozenset — the registry is the sole authority).
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


def _shipped_package_files(repo_root: Path = _REPO_ROOT) -> list[Path]:
    """Files under ``src/rebar`` that ship with the package.

    The enumeration source is ``git ls-files`` — deterministically the tracked set,
    which is what the sdist and wheel are built from. Untracked working-tree
    artifacts (vendored ``editor_assets/node_modules`` payloads, editor build
    outputs) do not ship, so a token inside one is not a shipped-surface defect.
    If git is unavailable the scan falls back to walking the whole tree: a
    *superset* of the tracked set, so the oracle is never silently narrowed.
    """
    package_root = repo_root / "src" / "rebar"
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z", "--", "src/rebar"],
        capture_output=True,
        check=False,
    )
    if completed.returncode == 0:
        tracked = [
            repo_root / name for name in completed.stdout.decode("utf-8").split("\0") if name
        ]
        return [path for path in tracked if path.is_file()]
    return [
        path
        for path in package_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    ]


def _scan_for_retired_tokens(
    paths: list[Path], repo_root: Path = _REPO_ROOT
) -> dict[str, list[str]]:
    """Map each scanned file that names a retired command to the commands it names.

    Files are matched as **bytes**, never decoded: the retired command tokens are
    ASCII, so a byte-substring search finds every occurrence a UTF-8 decode would
    have found, while a non-textual file (an ``esbuild`` Mach-O binary vendored
    under the package tree) merely fails to match instead of raising
    ``UnicodeDecodeError``. No decode error is caught or swallowed — none is
    possible.
    """
    needles = {command: command.encode("utf-8") for command in _RETIRED_BRIDGE_COMMANDS}
    matches: dict[str, list[str]] = {}
    for path in paths:
        data = path.read_bytes()
        found = [command for command, needle in needles.items() if needle in data]
        if found:
            matches[str(path.relative_to(repo_root))] = found
    return matches


def test_shipped_surfaces_name_no_retired_bridge_command() -> None:
    """The shipped package and active command-contract docs contain no stale token."""
    package_files = _shipped_package_files()
    active_docs = [
        _REPO_ROOT / "docs" / "cli-reference.md",
        _REPO_ROOT / "docs" / "exit-codes.md",
        _REPO_ROOT / "docs" / "output-schemas.md",
    ]
    # Guard the enumeration itself: an empty/garbled tracked set would make the
    # scan vacuously pass.
    assert any(path.name == "cli.py" for path in package_files), (
        "shipped-package enumeration lost the package sources"
    )

    assert _scan_for_retired_tokens([*package_files, *active_docs]) == {}


def _init_repo(root: Path) -> None:
    for args in (
        ["git", "init", "-q", "--initial-branch=main"],
        ["git", "config", "user.email", "t@e.com"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=root, check=True, capture_output=True)


def test_scan_tolerates_undecodable_file_but_still_reports_token(tmp_path: Path) -> None:
    """Negative control: a non-UTF-8 file is scanned without error; text still trips."""
    binary = tmp_path / "esbuild"
    binary.write_bytes(b"\xcf\xfa\xed\xfe\x00\x80\xff\xfe binary payload")
    clean = tmp_path / "clean.py"
    clean.write_text("bridge probe\n", encoding="utf-8")
    stale = tmp_path / "stale.md"
    stale.write_text(f"run `rebar {_RETIRED_BRIDGE_COMMANDS[0]}`\n", encoding="utf-8")

    assert _scan_for_retired_tokens([binary, clean], repo_root=tmp_path) == {}
    assert _scan_for_retired_tokens([binary, clean, stale], repo_root=tmp_path) == {
        "stale.md": [_RETIRED_BRIDGE_COMMANDS[0]]
    }


def test_shipped_enumeration_excludes_untracked_build_artifacts(tmp_path: Path) -> None:
    """Negative control: an ignored binary under src/rebar is not a shipped surface."""
    _init_repo(tmp_path)
    package = tmp_path / "src" / "rebar"
    (package / "editor_assets" / "node_modules" / ".bin").mkdir(parents=True)
    (package / "cli.py").write_text("# shipped\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(
        "src/rebar/editor_assets/node_modules/\n", encoding="utf-8"
    )
    artifact = package / "editor_assets" / "node_modules" / ".bin" / "esbuild"
    artifact.write_bytes(b"\xcf\xfa\xed\xfe\x00\x80\xff\xfe")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)

    found = _shipped_package_files(repo_root=tmp_path)

    assert [path.relative_to(tmp_path).as_posix() for path in found] == ["src/rebar/cli.py"]
    assert artifact not in found
