"""Cross-session warning detector + config toggle (epic concave-pale-sheldrake, story
fattish-sodium-lemming / 0804).

Oracle for the pure ``cross_session_warning`` detector (truth matrix + message content) and
the ``cross_session_warning_for`` convenience exercised through the REAL config read path
(``[warnings] cross_session`` default true). Asserts observable behavior only — the returned
string / ``None``, never internal structure.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands.cross_session import cross_session_warning, cross_session_warning_for

pytestmark = pytest.mark.unit

_SESSION_VARS = ("REBAR_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "OPENCODE_SESSION_ID", "SESSION_ID")


# ------------------------------------------------------------------ pure detector: warns
def test_warns_when_different_active_session() -> None:
    """Enabled, an acting id, a held ticket, and DIFFERENT ids -> a non-None warning that
    names the holder's claimed_session id."""
    state = {"claimed_session": "sess-A"}
    msg = cross_session_warning(state, acting_session="sess-B", enabled=True)
    assert msg is not None
    assert "sess-A" in msg


# ------------------------------------------------------------------ pure detector: suppression
@pytest.mark.parametrize(
    ("state", "acting", "enabled"),
    [
        ({"claimed_session": "sess-A"}, "sess-B", False),  # disabled
        ({"claimed_session": "sess-A"}, None, True),  # acting id unknown
        ({"claimed_session": None}, "sess-B", True),  # no holder (None)
        ({}, "sess-B", True),  # no holder (absent)
        ({"claimed_session": ""}, "sess-B", True),  # no holder (empty)
        ({"claimed_session": "sess-A"}, "sess-A", True),  # same session
    ],
)
def test_silent_for_each_suppression_case(state, acting, enabled) -> None:
    assert cross_session_warning(state, acting_session=acting, enabled=enabled) is None


# ------------------------------------------------------------------ pure detector: message content
def test_message_includes_harness_tag_when_present() -> None:
    state = {"claimed_session": "sess-A", "claim_harness": "claude-code"}
    msg = cross_session_warning(state, acting_session="sess-B", enabled=True)
    assert msg is not None
    assert "sess-A" in msg
    assert "claude-code" in msg


def test_message_omits_harness_when_absent() -> None:
    state = {"claimed_session": "sess-A"}
    msg = cross_session_warning(state, acting_session="sess-B", enabled=True)
    assert msg is not None
    assert "sess-A" in msg
    assert "harness" not in msg


# ------------------------------------------------------------------ convenience: real read path
@pytest.fixture
def rebar_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in _SESSION_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("AI_AGENT", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def test_cross_session_config_toggle(rebar_repo: Path, monkeypatch) -> None:
    """Real config read path: an A-held ticket acted on by B warns at the default and is
    SILENT with ``[warnings] cross_session=false`` in rebar.toml — proving the key is wired
    through the actual ``Config`` load, not just parsed."""
    from rebar.config import reset_config_cache

    # Claim as session A.
    monkeypatch.setenv("REBAR_SESSION_ID", "sess-A")
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="alice", repo_root=str(rebar_repo))

    # Now act as a DIFFERENT session B.
    monkeypatch.setenv("REBAR_SESSION_ID", "sess-B")

    # Default (toggle on): warns.
    reset_config_cache()
    warn_default = cross_session_warning_for(tid, tracker=None, repo_root=str(rebar_repo))
    assert warn_default is not None
    assert "sess-A" in warn_default

    # Toggle off via rebar.toml: silent.
    (rebar_repo / "rebar.toml").write_text("[warnings]\ncross_session = false\n", encoding="utf-8")
    reset_config_cache()
    warn_off = cross_session_warning_for(tid, tracker=None, repo_root=str(rebar_repo))
    assert warn_off is None
