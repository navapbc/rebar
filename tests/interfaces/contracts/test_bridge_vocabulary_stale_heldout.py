"""Repository-wide guard for uncategorized legacy bridge command vocabulary."""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

import pytest
from adapters import _unwrap

import rebar

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
    "src/rebar/_cli/_registry.py",
    "src/rebar/_cli/help/bridge-fsck.txt",
    "src/rebar/_cli/help/bridge-probe.txt",
    "src/rebar/_engine_support/bridge_fsck.py",
    "scripts/gen_cli_reference.py",
    "scripts/build_cloud_adf_corpus.py",
    "scripts/build_dc_wiki_corpus.py",
    "docs/exit-codes.md",
    "docs/user-guide.md",
    "tests/interfaces/contracts/test_help_overview_coverage.py",
    "tests/interfaces/contracts/test_schema_coverage.py",
    "tests/unit/test_cli_registry.py",
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


@pytest.mark.parametrize(
    ("legacy", "primary"),
    [
        ("bridge-fsck", "bridge fsck"),
        ("bridge-probe", "bridge check-access"),
        ("jira-onboard", "bridge setup"),
    ],
)
def test_release_notes_map_each_compatibility_spelling_to_its_primary(
    legacy: str, primary: str
) -> None:
    """The migration contract pairs each retained alias with its replacement."""
    release_notes = (_REPO_ROOT / "docs/release-notes.md").read_text(encoding="utf-8")
    assert "primary operator spellings" in release_notes.lower()
    assert re.search(
        rf"`rebar {re.escape(legacy)}`\s*->\s*`rebar {re.escape(primary)}`",
        release_notes,
    )


def test_library_and_mcp_bridge_fsck_entrypoints_remain_callable(rebar_repo: Path) -> None:
    """The CLI rename does not rename or detach either public programmatic surface."""
    from rebar.mcp_server import build_server

    expected_keys = {"unknown_event_types", "binding_drift", "store_integrity"}
    library_result = rebar.bridge_fsck(repo_root=rebar_repo)
    assert set(library_result) == expected_keys
    assert library_result["unknown_event_types"] == []
    assert library_result["store_integrity"] == []

    result = _unwrap(asyncio.run(build_server().call_tool("bridge_fsck", {})))
    assert set(result) == expected_keys
    assert result == library_result


def test_library_and_mcp_bridge_fsck_surface_operational_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Programmatic compatibility surfaces fail closed with the shared error identity."""
    from mcp.server.fastmcp.exceptions import ToolError

    from rebar._mcp_errors import McpEnvelopeError
    from rebar.mcp_server import build_server

    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tracker, check=True)

    with pytest.raises(rebar.RebarError) as library_error:
        rebar.bridge_fsck(repo_root=tmp_path)
    assert library_error.value.returncode == 2
    assert "tickets" in str(library_error.value).lower()

    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    for var in ("REBAR_TRACKER_DIR", "REBAR_TRACKER_BRANCH", "REBAR_CONFIG"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(ToolError) as mcp_error:
        asyncio.run(build_server().call_tool("bridge_fsck", {}))
    cause = mcp_error.value.__cause__
    assert isinstance(cause, McpEnvelopeError)
    assert cause.envelope["error"] == "command_failed"
    assert "tickets" in cause.envelope["message"].lower()
