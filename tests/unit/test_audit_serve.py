"""Story a3d7 (cultivated-aquatic-crow): ``rebar audit serve`` — HAPPY PATH.

The audit UI's server-scaffolding vertical slice: a default-off ``[ui] enabled``
config flag, a ``nava-rebar[ui]`` extra, a lazily-imported FastAPI server, and a
``rebar audit serve`` subcommand serving a read-only index of tickets that have
audit data.

This file holds the HAPPY-PATH oracle shared with the implementer:

* ``[ui] enabled`` config round-trips (rebar.toml / env / -c) and is a recognised
  section (not an "unknown key").
* the enabled server's index route (built via ``create_app``) returns HTTP 200 and
  lists a ticket that has audit data.

Edge/E2E behaviour (disabled-refusal, missing-extra message, non-loopback warning,
real loopback bind on an ephemeral port) lives in the held-out companion suite.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import config as cfg
from rebar.llm.plan_review import sidecar as plan_sidecar

pytestmark = pytest.mark.unit


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clean_ui_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ambient config so each test sees only what it sets up."""
    monkeypatch.delenv("REBAR_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("REBAR_UI_ENABLED", raising=False)
    cfg.set_cli_overrides(None)
    cfg.reset_config_cache()


def _proj(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir()
    (p / ".git").mkdir()  # boundary marker
    return p


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real, initialised rebar store (mirrors tests/unit/test_audit_read.py)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "k")
    rebar.init_repo(repo_root=str(repo))
    return repo


def _plan_verdict(tid: str, text: str = "a finding") -> dict:
    return {
        "verdict": "PASS",
        "ticket_id": tid,
        "ticket_type": "task",
        "advisory": [{"id": "f1", "finding": text, "criteria": ["T1"], "decision": "advisory"}],
        "coverage": {"metrics": {}},
        "coaching": [],
    }


# ── AC1: [ui] enabled config round-trip (recognised section) ─────────────────
def test_ui_enabled_via_rebar_toml(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[ui]\nenabled = true\n", encoding="utf-8")
    assert cfg.load_config(root=p).ui.enabled is True


def test_ui_enabled_via_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_UI_ENABLED", "true")
    assert cfg.load_config(root=_proj(tmp_path)).ui.enabled is True


def test_ui_enabled_via_cli_override(tmp_path: Path) -> None:
    c = cfg.load_config(root=_proj(tmp_path), cli_overrides={"ui": {"enabled": "true"}})
    assert c.ui.enabled is True


def test_ui_default_disabled(tmp_path: Path) -> None:
    assert cfg.load_config(root=_proj(tmp_path)).ui.enabled is False


def test_ui_section_is_not_an_unknown_key(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Loading ``[ui] enabled`` must NOT emit an unknown-section/-key warning."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[ui]\nenabled = true\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="rebar.config"):
        cfg.load_config(root=p)
    offending = [
        r.getMessage()
        for r in caplog.records
        if "unknown" in r.getMessage().lower() and "ui" in r.getMessage().lower()
    ]
    assert not offending, f"[ui] wrongly reported as unknown: {offending}"


# ── AC6 (core): the enabled server's index lists an audited ticket ───────────
def test_index_lists_a_ticket_with_audit_data(store: Path) -> None:
    """``create_app(repo_root=...)`` serves an index (HTTP 200) that lists a
    ticket which has audit data (here: a plan-review sidecar)."""
    pytest.importorskip("fastapi")  # the [ui] extra; absent in the lean CI suite
    pytest.importorskip("httpx")  # starlette TestClient's HTTP backend
    from starlette.testclient import TestClient

    from rebar.audit import server

    r = str(store)
    tid = rebar.create_ticket("task", "audited work ticket", description="x" * 60, repo_root=r)
    assert plan_sidecar.emit(_plan_verdict(tid), material="m1", repo_root=r)

    app = server.create_app(repo_root=r)
    client = TestClient(app)
    resp = client.get("/")

    assert resp.status_code == 200
    body = resp.text
    assert tid in body  # the audited ticket is listed
    assert f"/ticket/{tid}" in body  # …and links to its per-ticket page
