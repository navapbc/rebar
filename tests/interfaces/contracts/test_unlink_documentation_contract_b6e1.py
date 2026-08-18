"""Public documentation parity for optional relation-scoped unlink (bug b6e1).

The operation has two supported modes: omission keeps the historical most-recent
pair fallback, while an explicit canonical relation removes only that relation.
This contract is observable through shipped CLI help and the maintained model/event
documentation, not through implementation-source inspection.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.interface

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OPTIONAL_USAGE = re.compile(r"unlink\s+<source>\s+<target>\s+\[relation\]", re.IGNORECASE)


def _assert_two_mode_contract(text: str, *, surface: str) -> None:
    usage = _OPTIONAL_USAGE.search(text)
    assert usage, f"{surface} omits the optional [relation] selector"
    tail = re.sub(r"[*`_]", "", text[usage.start() :]).lower()
    tail = " ".join(tail.split())
    omission_is_named = any(
        marker in tail for marker in ("no relation", "without a relation", "relation is omitted")
    )
    assert omission_is_named and "most-recent" in tail, (
        f"{surface} omits the relation-less most-recent fallback"
    )
    assert "relation" in tail and re.search(r"\bexact(?:ly)?\b", tail), (
        f"{surface} omits exact relation-scoped removal"
    )


def _paragraph_containing(path: Path, marker: str) -> str:
    paragraphs = re.split(r"\n\s*\n", path.read_text(encoding="utf-8"))
    matches = [paragraph for paragraph in paragraphs if marker in paragraph]
    assert matches, f"{path.relative_to(_REPO_ROOT)} lost the {marker!r} contract paragraph"
    return matches[0]


def test_packaged_top_level_help_describes_both_unlink_modes() -> None:
    """The installed overview must agree with the already-correct command help."""
    command_help = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "unlink", "--help"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert command_help.returncode == 0, command_help.stderr
    _assert_two_mode_contract(command_help.stdout, surface="unlink command help precondition")

    overview = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "--help"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert overview.returncode == 0, overview.stderr
    unlink_line = next(
        (line for line in overview.stdout.splitlines() if line.lstrip().startswith("unlink ")),
        "",
    )
    assert unlink_line, "top-level packaged help lost the unlink command"
    _assert_two_mode_contract(unlink_line, surface="top-level packaged help")


def test_ticket_model_describes_both_unlink_modes() -> None:
    paragraph = _paragraph_containing(
        _REPO_ROOT / "docs" / "ticket-model.md", "`unlink <source> <target>"
    )
    _assert_two_mode_contract(paragraph, surface="ticket model")


def test_event_schema_distinguishes_selector_from_uuid_cancellation() -> None:
    schema = (_REPO_ROOT / "docs" / "event-schema.md").read_text(encoding="utf-8")
    row = next((line for line in schema.splitlines() if "| `LINK` / `UNLINK` |" in line), "")
    assert row, "event schema lost the LINK / UNLINK contract row"
    _assert_two_mode_contract(row, surface="event schema")
    assert re.search(r"UNLINK.{0,160}link_uuid", row, re.IGNORECASE), (
        "event schema must explain that selector resolution emits an UNLINK which cancels "
        "the selected LINK by link_uuid"
    )
