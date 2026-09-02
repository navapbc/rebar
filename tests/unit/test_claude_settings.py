"""Guards on the TRACKED ``.claude/settings.json`` (story dynamic-mobile-bird, epic
frail-tsarist-trout): the project-level Claude Code setting that keeps MCP tool schemas —
Serena's symbol tools among them — resident instead of deferred behind a ``ToolSearch``
discovery step.

These are static file guards: no session, no network, no billable call. They exist because
the setting is a single JSON key whose typo modes are all SILENT — a misspelt key, a
boolean instead of a string, or the ``.gitignore`` negation drifting would each leave the
file present and the intent unmet.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.allow_unharnessed_subprocess(
    "the shared `_git` helper asks git about THIS checkout's committed .claude/settings.json"
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SETTINGS = _REPO_ROOT / ".claude" / "settings.json"

# The documented ``ENABLE_TOOL_SEARCH`` vocabulary. "true" is deliberately EXCLUDED: it
# forces deferral always, which is the opposite of this story's intent.
_NON_DEFERRING = {"false"}


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=_REPO_ROOT, capture_output=True, text=True, check=False
    )


def _settings() -> dict:
    return json.loads(_SETTINGS.read_text(encoding="utf-8"))


# ── happy path ──────────────────────────────────────────────────────────────────────
def test_settings_file_sets_a_non_deferring_tool_search_value():
    """The tracked project settings keep MCP tool schemas loaded (no ToolSearch step)."""
    assert _SETTINGS.is_file(), f"{_SETTINGS} is missing"
    value = _settings().get("env", {}).get("ENABLE_TOOL_SEARCH")
    assert value in _NON_DEFERRING, (
        f"env.ENABLE_TOOL_SEARCH is {value!r}; expected one of {sorted(_NON_DEFERRING)} so "
        "Serena's symbol tools are resident rather than deferred"
    )


# ── edge / contract ─────────────────────────────────────────────────────────────────
def test_tool_search_value_is_a_string_not_a_bool():
    """Claude Code reads ``env`` values as strings; a JSON ``false`` is not the same thing."""
    value = _settings()["env"]["ENABLE_TOOL_SEARCH"]
    assert isinstance(value, str), (
        f"env.ENABLE_TOOL_SEARCH must be the STRING {'false'!r}, got {type(value).__name__} "
        f"{value!r} — a JSON boolean is silently not the documented form"
    )


def test_settings_file_is_tracked_by_git():
    """The point of the story is a SHARED default; an untracked file helps nobody."""
    proc = _git("ls-files", "--error-unmatch", ".claude/settings.json")
    assert proc.returncode == 0, (
        ".claude/settings.json is not tracked by git — the .gitignore negation is missing "
        f"or wrong (git said: {proc.stderr.strip()})"
    )


@pytest.mark.parametrize(
    "path",
    [".claude/settings.local.json", ".claude/scratch/anything.txt", ".claude/worktrees/x"],
)
def test_operator_local_claude_paths_stay_ignored(path: str):
    """The negation must expose EXACTLY settings.json — operator-local state stays ignored."""
    assert _git("check-ignore", "-q", path).returncode == 0, (
        f"{path} is no longer git-ignored: the .gitignore negation is too broad and would "
        "sweep operator-local Claude Code state into the repo"
    )


def test_settings_file_does_not_grant_extra_autonomy():
    """Scope guard: this story ships ONE env key, not permission or autonomy changes."""
    settings = _settings()
    assert "permissions" not in settings, (
        "this file must not carry a permissions block — broadening tool autonomy is not in "
        "this story's scope and would ride in unreviewed on a context-cost change"
    )
    assert set(settings["env"]) == {"ENABLE_TOOL_SEARCH"}, (
        f"unexpected env keys: {sorted(set(settings['env']) - {'ENABLE_TOOL_SEARCH'})}"
    )


def test_docs_record_the_cost_and_the_revert():
    """A context-cost tradeoff nobody can find or undo is not a documented tradeoff."""
    doc = (_REPO_ROOT / "docs" / "local-dev-env.md").read_text(encoding="utf-8")
    assert "ENABLE_TOOL_SEARCH" in doc, "docs/local-dev-env.md never names the setting"
    assert "revert" in doc.lower(), "docs/local-dev-env.md does not say how to revert it"
    # The measured numbers are the justification for the chosen value; without them the
    # tradeoff is an assertion rather than evidence.
    assert "63,852" in doc or "63852" in doc or "100,813" in doc, (
        "docs/local-dev-env.md does not record the MEASURED prompt-prefix cost of the "
        "setting, so a future reader cannot re-evaluate the tradeoff"
    )
