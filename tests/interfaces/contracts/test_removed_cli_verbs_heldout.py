"""Held-out guard: removed CLI spellings stay removed (epic toadyish-sulfurous-ovenbird).

Two contracts, both driven from the single registry ``_help_route._REMOVED_SPELLINGS`` so no
copy of the list can drift:

* every removed spelling resolves to no route, fails through the real ``main()`` with exit 2,
  argparse's invalid-choice error and a hint, and the hint names a LIVE command (a top-level
  route, and for ``rebar store <sub>`` a member of ``STORE_ROUTES``);
* no teaching surface outside frozen history writes ``rebar <removed spelling>``.
"""

from __future__ import annotations

import argparse
import importlib
import re
import subprocess
from pathlib import Path

import pytest

from rebar._cli import _registry, main
from rebar._cli._help_route import _REMOVED_SPELLINGS

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Teaching surfaces: what a human or agent reads to learn the CLI, plus the scripts and CI
# definitions that invoke it.
_SCANNED_PREFIXES = (
    "docs/",
    "templates/",
    "examples/",
    "src/rebar/",
    "scripts/",
    ".github/",
)
_SCANNED_FILES = {"AGENTS.md", "CONTRIBUTING.md", "README.md"}
# Frozen history keeps the words of its time.
_HISTORY_PREFIXES = (
    "docs/adr/",
    "docs/archive/",
    "docs/experiments/",
    "reports/",
)
# Files whose job is to name the removed spellings: the change records and the registry.
_WHOLE_FILE_ALLOWLIST = {
    "CHANGELOG.md",
    "docs/release-notes.md",
    "docs/exit-codes.md",
    "src/rebar/_cli/_help_route.py",
}
_OLD = re.compile(
    r"(?<![\w.\-])rebar ("
    + "|".join(sorted(map(re.escape, _REMOVED_SPELLINGS), key=len, reverse=True))
    + r")(?![\w\-])"
)


def _names_removed_spelling(line: str) -> bool:
    """Whether ``line`` writes ``rebar <removed spelling>``.

    Only the Python statement ``from rebar import …`` is exempt: an earlier blanket
    ``(?<!from )`` lookbehind also hid prose such as "tickets from rebar export NDJSON".
    """
    for match in _OLD.finditer(line):
        if match.group(1) == "import" and line[: match.start()].endswith("from "):
            continue
        return True
    return False


def _subcommand_choices(route_name: str) -> frozenset[str]:
    """The nested subcommand names a top-level route's parser accepts (empty if none)."""
    route = _registry.route_for(route_name)
    assert route is not None and route.parser_factory, route_name
    module, _, attr = route.parser_factory.partition(":")
    parser = getattr(importlib.import_module(module), attr)(prog=f"rebar {route_name}")
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return frozenset(action.choices)
    return frozenset()


@pytest.mark.parametrize("old", sorted(_REMOVED_SPELLINGS))
def test_removed_spelling_fails_loudly_with_a_live_replacement(
    old: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _registry.route_for(old) is None
    assert main([old]) == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err and repr(old) in err
    replacement = _REMOVED_SPELLINGS[old]
    assert f"hint: 'rebar {old}' was removed; use `{replacement}` instead" in err

    words = replacement.split()
    assert words[0] == "rebar", replacement
    head = _registry.route_for(words[1])
    assert head is not None and not head.retired, replacement
    # A nested family (``bridge``, ``store``) must accept the named sub-verb too.
    nested = _subcommand_choices(words[1])
    if nested:
        assert len(words) > 2 and words[2] in nested, (replacement, sorted(nested))
    if words[1] == "store":
        assert _registry.route_for(words[2], _registry.STORE_ROUTES) is not None, replacement


def _scanned(relative: str) -> bool:
    if relative in _WHOLE_FILE_ALLOWLIST or relative.startswith(_HISTORY_PREFIXES):
        return False
    return relative in _SCANNED_FILES or relative.startswith(_SCANNED_PREFIXES)


def test_no_teaching_surface_writes_a_removed_spelling() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=_REPO_ROOT, capture_output=True, check=True
    ).stdout.split(b"\0")
    offenders: dict[str, list[int]] = {}
    for raw in tracked:
        relative = raw.decode()
        if not relative or not _scanned(relative):
            continue
        path = _REPO_ROOT / relative
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lines = [n for n, line in enumerate(text.splitlines(), 1) if _names_removed_spelling(line)]
        if lines:
            offenders[relative] = lines
    assert offenders == {}, f"teaching surfaces name a removed CLI spelling: {offenders}"


def test_scan_pattern_matches_invocations_not_imports() -> None:
    assert _names_removed_spelling("run `rebar fsck --repair` to converge")
    assert _names_removed_spelling("rebar compact-all --dry-run")
    assert _names_removed_spelling("Import tickets from rebar export NDJSON into this repo.")
    assert not _names_removed_spelling("from rebar import fsck")
    assert not _names_removed_spelling("rebar store fsck")
    assert not _names_removed_spelling("python -m rebar.cli fsck")


def test_bridge_family_exposes_its_subcommands() -> None:
    """Guards the nested check above against a parser shape it cannot see."""
    assert {"status", "fsck", "check-access", "setup"} <= _subcommand_choices("bridge")
    assert {"fsck", "maintenance", "footprint", "compact"} <= _subcommand_choices("store")
