"""Story a3d7 (cultivated-aquatic-crow): ``rebar audit serve`` — HELD-OUT oracle.

These edge/E2E tests are withheld from the implementation subagent (which sees only
the happy-path suite ``test_audit_serve.py``). They separate a real implementation
from one that fakes the happy path:

* AC2 — disabled (default) refuses to start, NAMING the ``[ui] enabled`` flag, and
  ``import rebar`` pulls no web dependency.
* AC3 — with the ``ui`` extra absent, ``audit serve`` exits with an actionable
  "install nava-rebar[ui]" message (not a traceback).
* AC4 — ``--host 0.0.0.0`` warns on stderr before binding; ``--host 127.0.0.1``
  (default) does not.
* AC5/AC6 — with the feature enabled, the server binds 127.0.0.1 on the given
  ``--port`` (ephemeral) and serves an index (HTTP 200) listing a seeded ticket.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

import rebar
from rebar._cli._audit_commands import audit_cli
from rebar.llm.plan_review import sidecar as plan_sidecar

pytestmark = pytest.mark.unit


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
    monkeypatch.delenv("REBAR_UI_ENABLED", raising=False)
    from rebar import config as cfg

    cfg.set_cli_overrides(None)
    cfg.reset_config_cache()
    rebar.init_repo(repo_root=str(repo))
    return repo


def _plan_verdict(tid: str) -> dict:
    return {
        "verdict": "PASS",
        "ticket_id": tid,
        "ticket_type": "task",
        "advisory": [{"id": "f1", "finding": "x", "criteria": ["T1"], "decision": "advisory"}],
        "coverage": {"metrics": {}},
        "coaching": [],
    }


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── AC2: disabled by default → refuse, naming the flag; no web import ────────
def test_serve_disabled_refuses_and_names_flag(
    store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With ``[ui] enabled`` false (default) ``audit serve`` returns non-zero and
    its message names the flag — and it starts no server."""
    rc = audit_cli(["serve"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "ui.enabled" in err or "[ui] enabled" in err or "[ui]" in err


def test_import_rebar_pulls_no_web_dependency() -> None:
    """``import rebar`` (and ``import rebar.audit``) must not import fastapi/uvicorn
    — the disabled/absent-extra path stays inert (mirrors test_core_optionality)."""
    code = (
        "import sys, rebar, rebar.audit;"
        "leaked=[m for m in ('fastapi','uvicorn','jinja2','starlette') if m in sys.modules];"
        "print('LEAK:'+','.join(leaked) if leaked else 'CLEAN')"
    )
    cp = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "CLEAN", f"web deps leaked on import: {cp.stdout.strip()}"


# ── AC3: ui extra absent → actionable message, not a traceback ───────────────
def test_serve_missing_extra_is_actionable(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], block_extra
) -> None:
    """Enabled, but the ``ui`` extra (fastapi) absent → an actionable install
    message referencing ``nava-rebar[ui]`` and a non-zero exit, no traceback."""
    monkeypatch.setenv("REBAR_UI_ENABLED", "1")
    from rebar import config as cfg

    cfg.reset_config_cache()
    block_extra("fastapi", "uvicorn", "starlette")
    rc = audit_cli(["serve", "--port", str(_free_port())])
    out = capsys.readouterr()
    assert rc != 0
    assert "nava-rebar[ui]" in (out.err + out.out)
    assert "Traceback" not in (out.err + out.out)


# ── AC4: non-loopback host warns; loopback does not ──────────────────────────
def test_serve_nonloopback_host_warns(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REBAR_UI_ENABLED", "1")
    from rebar import config as cfg

    cfg.reset_config_cache()
    from rebar.audit import server

    called: dict = {}
    monkeypatch.setattr(server, "serve", lambda **kw: called.update(kw))
    rc = audit_cli(["serve", "--host", "0.0.0.0", "--port", str(_free_port())])
    err = capsys.readouterr().err
    assert rc == 0
    assert "0.0.0.0" in err or "non-loopback" in err.lower() or "warning" in err.lower()
    assert called.get("host") == "0.0.0.0"


def test_serve_loopback_host_does_not_warn(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REBAR_UI_ENABLED", "1")
    from rebar import config as cfg

    cfg.reset_config_cache()
    from rebar.audit import server

    monkeypatch.setattr(server, "serve", lambda **kw: None)
    rc = audit_cli(["serve", "--host", "127.0.0.1", "--port", str(_free_port())])
    err = capsys.readouterr().err
    assert rc == 0
    assert "warning" not in err.lower() and "non-loopback" not in err.lower()


# ── AC5/AC6: real loopback bind on an ephemeral port serves the index ────────
@pytest.mark.allow_network
def test_serve_binds_loopback_ephemeral_and_lists_ticket(store: Path) -> None:
    """A REAL server (uvicorn) bound to 127.0.0.1 on an ephemeral ``--port`` returns
    HTTP 200 on the index and lists a seeded audited ticket."""
    pytest.importorskip("fastapi")  # the [ui] extra; absent in the lean CI suite
    uvicorn = pytest.importorskip("uvicorn")

    from rebar.audit import server

    r = str(store)
    tid = rebar.create_ticket("task", "live audited ticket", description="x" * 60, repo_root=r)
    assert plan_sidecar.emit(_plan_verdict(tid), material="m1", repo_root=r)

    port = _free_port()
    app = server.create_app(repo_root=r)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    uv = uvicorn.Server(config)
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not uv.started and time.time() < deadline:
            time.sleep(0.05)
        assert uv.started, "server did not start within 10s"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
            assert resp.status == 200
            body = resp.read().decode()
        assert tid in body
    finally:
        uv.should_exit = True
        thread.join(timeout=5)
