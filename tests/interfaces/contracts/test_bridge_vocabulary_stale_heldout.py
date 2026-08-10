"""Repository-wide guard for uncategorized legacy bridge command vocabulary."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]

_OLD = re.compile(r"(?<![A-Za-z0-9-])(bridge-fsck|bridge-probe|jira-onboard)(?![A-Za-z0-9-])")
_HISTORY_PREFIXES = (
    "docs/adr/",
    "docs/archive/",
    "docs/experiments/",
)
_WHOLE_FILE_ALLOWLIST = {
    "CHANGELOG.md",
    "docs/release-notes.md",
    "tests/interfaces/contracts/test_bridge_vocabulary_stale_heldout.py",
    "tests/interfaces/facades/test_bridge_vocabulary.py",
    "tests/interfaces/facades/test_bridge_vocabulary_heldout.py",
    "tests/interfaces/facades/test_bridge_vocabulary_e2e_heldout.py",
}
_COMPATIBILITY_FILES = {
    "docs/cli-reference.md",
    "src/rebar/_cli/__init__.py",
    "src/rebar/_cli/_jira_onboard.py",
    "src/rebar/_cli/help/bridge-fsck.txt",
    "src/rebar/_cli/help/bridge-probe.txt",
    "src/rebar/_engine_support/bridge_fsck.py",
    "scripts/gen_cli_reference.py",
    "docs/exit-codes.md",
    "docs/user-guide.md",
    "tests/interfaces/contracts/test_help_overview_coverage.py",
    "tests/interfaces/contracts/test_schema_coverage.py",
    "tests/unit/test_gen_cli_reference.py",
    "tests/unit/test_jira_onboard.py",
}


def test_no_uncategorized_live_old_spelling() -> None:
    """Every retained old spelling belongs to compatibility or immutable history."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    offenders: dict[str, list[int]] = {}

    for raw in tracked:
        if not raw:
            continue
        relative = raw.decode()
        if relative in _WHOLE_FILE_ALLOWLIST or relative.startswith(_HISTORY_PREFIXES):
            continue
        path = _REPO_ROOT / relative
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lines = [number for number, line in enumerate(text.splitlines(), 1) if _OLD.search(line)]
        if lines and relative not in _COMPATIBILITY_FILES:
            offenders[relative] = lines

    assert offenders == {}


def test_primary_docs_present_the_canonical_spellings() -> None:
    """Compatibility notes cannot displace the new primary vocabulary."""
    primary = "\n".join(
        (_REPO_ROOT / path).read_text(encoding="utf-8")
        for path in (
            "docs/cli-reference.md",
            "docs/jira-sync-setup.md",
            "docs/user-guide.md",
            "src/rebar/_cli/help/bridge.txt",
        )
    )
    for spelling in ("bridge fsck", "bridge check-access", "bridge setup"):
        assert spelling in primary
