"""Session-provenance capture shims (stories ec5c / S4, 7656 / S6).

Validates the copy-paste hook scripts are correct shell and produce the right env-file
exports, plus the install docs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHIMS = _REPO_ROOT / "scripts" / "session-shims"
_CLAUDE_HOOK = _SHIMS / "claude-code-session-start.sh"
_DOCS = _REPO_ROOT / "docs" / "session-id-shims.md"
_CONFIG_DOC = _REPO_ROOT / "docs" / "config.md"
_EVENT_DOC = _REPO_ROOT / "docs" / "event-schema.md"
_SESSION_ID_SRC = _REPO_ROOT / "src" / "rebar" / "_commands" / "session_id.py"


def _run_hook(script: Path, stdin: str, env_file: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)],
        input=stdin,
        capture_output=True,
        text=True,
        env={"CLAUDE_ENV_FILE": str(env_file), "PATH": _os_path()},
    )


def _os_path() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")


# ------------------------------------------------------------------ Claude Code hook
def test_claude_code_hook_is_valid_shell() -> None:
    assert _CLAUDE_HOOK.exists()
    r = subprocess.run(["bash", "-n", str(_CLAUDE_HOOK)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_claude_code_hook_exports_session_and_harness(tmp_path) -> None:
    env_file = tmp_path / "env"
    env_file.touch()
    r = _run_hook(_CLAUDE_HOOK, '{"session_id":"abc123","source":"startup"}', env_file)
    assert r.returncode == 0, r.stderr
    written = env_file.read_text(encoding="utf-8")
    assert "export REBAR_SESSION_ID=abc123" in written
    assert "export AI_AGENT=claude-code" in written


def test_claude_code_hook_idempotent_on_refire(tmp_path) -> None:
    """Re-firing (e.g. resume, new session_id per G1) replaces this hook's OWN prior exports
    rather than accumulating them, and preserves other hooks' lines."""
    env_file = tmp_path / "env"
    env_file.write_text("export OTHER_HOOK_VAR=keepme\n", encoding="utf-8")
    _run_hook(_CLAUDE_HOOK, '{"session_id":"first"}', env_file)
    _run_hook(_CLAUDE_HOOK, '{"session_id":"second"}', env_file)
    text = env_file.read_text(encoding="utf-8")
    assert text.count("export REBAR_SESSION_ID=") == 1, text  # no accumulation
    assert "export REBAR_SESSION_ID=second" in text  # current id wins
    assert "export REBAR_SESSION_ID=first" not in text
    assert text.count("export AI_AGENT=") == 1
    assert "export OTHER_HOOK_VAR=keepme" in text  # other hooks' lines preserved


def test_claude_code_hook_exit0_on_unwritable_env_file(tmp_path) -> None:
    """A CLAUDE_ENV_FILE that cannot be written (path under a non-existent dir) must NOT block
    the session: the hook still exits 0 (never a non-zero that halts Claude Code startup)."""
    bad = tmp_path / "does-not-exist" / "env"  # parent dir absent -> writes fail
    r = _run_hook(_CLAUDE_HOOK, '{"session_id":"abc"}', bad)
    assert r.returncode == 0, r.stderr


def test_claude_code_hook_malformed_json_is_noop(tmp_path) -> None:
    """Unparseable stdin must NOT block the session: no-op, exit 0 (robust parsing)."""
    env_file = tmp_path / "env"
    env_file.touch()
    r = _run_hook(_CLAUDE_HOOK, "not json at all {{{", env_file)
    assert r.returncode == 0
    assert env_file.read_text(encoding="utf-8") == ""


def test_claude_code_hook_noop_without_session_id(tmp_path) -> None:
    env_file = tmp_path / "env"
    env_file.touch()
    r = _run_hook(_CLAUDE_HOOK, '{"source":"startup"}', env_file)
    assert r.returncode == 0
    assert env_file.read_text(encoding="utf-8") == ""


def test_claude_code_hook_noop_without_env_file(tmp_path) -> None:
    import os

    r = subprocess.run(
        ["bash", str(_CLAUDE_HOOK)],
        input='{"session_id":"abc"}',
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},  # no CLAUDE_ENV_FILE
    )
    assert r.returncode == 0


# ------------------------------------------------------------------ vocabulary alignment
def test_harness_vocabulary_is_base_name_form() -> None:
    """All three edit targets (config.md, event-schema.md, session_id.py) present the AI_AGENT
    harness tag as a base name with an OPTIONAL `_<version>` suffix — NOT the old always-
    versioned `claude-code_<ver>` slash-list form (story ec5c alignment)."""
    cfg = _CONFIG_DOC.read_text(encoding="utf-8")
    assert "claude-code_<ver>` / `opencode" not in cfg
    assert "base name `claude-code`" in cfg

    ev = _EVENT_DOC.read_text(encoding="utf-8")
    assert "claude-code_<ver>` / `opencode" not in ev
    assert "base name `claude-code`" in ev

    src = _SESSION_ID_SRC.read_text(encoding="utf-8")
    assert 'claude-code_<ver>" / "opencode' not in src
    assert 'base name "claude-code"' in src


# ------------------------------------------------------------------ docs
def test_docs_document_claude_code_shim() -> None:
    doc = _DOCS.read_text(encoding="utf-8")
    assert "SessionStart" in doc
    assert "scripts/session-shims/claude-code-session-start.sh" in doc
    assert "REBAR_SESSION_ID" in doc
    assert '"hooks"' in doc and '"SessionStart"' in doc  # the settings.json install snippet
    # Both required caveats, anchored to specific phrasing (whitespace-normalised so doc
    # line-wrapping is irrelevant; avoids substring false-positive).
    flat = " ".join(doc.replace(">", " ").split())  # drop blockquote markers too
    assert "NOT stable across `--resume` / `--continue` / fork / subagents" in flat  # G1
    assert "do NOT `export REBAR_SESSION_ID=…` from a shell profile" in flat  # G3 not-in-profile
    # The "Verifying the hook" section must carry the actual verification STEPS, not just a heading.
    assert "Verifying the hook" in doc
    assert 'echo "$REBAR_SESSION_ID"' in doc
    assert "claimed_session" in doc and "claim_harness" in doc
