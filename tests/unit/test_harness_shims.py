"""Codex + Cursor capture shims (story 7656 / S6).

Validates the copy-paste config templates + install script: valid TOML/JSON/shell, that each
reliably sets the AI_AGENT harness tag (the only env-injection point those harnesses actually
offer), Cursor-script idempotency, and the install docs (real mechanism + cited vendor URLs).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import tomllib

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHIMS = _REPO_ROOT / "scripts" / "session-shims"
_CODEX_TOML = _SHIMS / "codex-config.toml"
_CURSOR_JSON = _SHIMS / "cursor-environment.json"
_CURSOR_SH = _SHIMS / "cursor-provenance.sh"
_DOCS = _REPO_ROOT / "docs" / "session-id-shims.md"


# ------------------------------------------------------------------ Codex
def test_codex_config_is_valid_toml_and_sets_harness() -> None:
    """The Codex fragment is valid TOML and sets AI_AGENT=codex via shell_environment_policy.set
    — the only reliable Codex env-injection point (SessionStart hooks cannot export env)."""
    assert _CODEX_TOML.exists()
    data = tomllib.loads(_CODEX_TOML.read_text(encoding="utf-8"))
    assert data["shell_environment_policy"]["set"]["AI_AGENT"] == "codex"


# ------------------------------------------------------------------ Cursor
def test_cursor_environment_json_runs_provenance() -> None:
    assert _CURSOR_JSON.exists()
    data = json.loads(_CURSOR_JSON.read_text(encoding="utf-8"))
    assert "cursor-provenance.sh" in data["install"]


def test_cursor_provenance_valid_and_idempotent(tmp_path) -> None:
    """Valid shell; running it twice leaves EXACTLY ONE `export AI_AGENT=cursor` in EACH of the
    VM profiles it targets (~/.bashrc AND ~/.profile) — no duplicate accumulation."""
    assert _CURSOR_SH.exists()
    assert subprocess.run(["bash", "-n", str(_CURSOR_SH)]).returncode == 0
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    for _ in range(2):
        r = subprocess.run(["bash", str(_CURSOR_SH)], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
    for rc_name in (".bashrc", ".profile"):
        rc = (home / rc_name).read_text(encoding="utf-8")
        assert rc.count("export AI_AGENT=cursor") == 1, f"{rc_name}: {rc!r}"


# ------------------------------------------------------------------ docs
def test_docs_document_codex_and_cursor_shims() -> None:
    doc = _DOCS.read_text(encoding="utf-8")
    # Codex: real injection point + limitation + source cite.
    assert "shell_environment_policy" in doc
    assert "scripts/session-shims/codex-config.toml" in doc
    assert "developers.openai.com/codex" in doc
    # Cursor: environment.json install + Secrets tab + source cite.
    assert "scripts/session-shims/cursor-environment.json" in doc
    assert "Secrets" in doc
    assert "cursor.com/docs/cloud-agent" in doc
