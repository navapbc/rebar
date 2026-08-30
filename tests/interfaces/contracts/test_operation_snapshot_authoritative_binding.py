"""Interface oracle for RP-04 S2 — making the operation snapshot AUTHORITATIVE for
CLI, command, and store operations (ticket 3a08-4016-0a3a-4be0).

Unlike the S1 shadow oracle (``test_operation_snapshot_surfaces.py``, ticket a377),
which proves every surface merely COMPOSES an equivalent snapshot as a side,
diagnostic effect, this file proves the snapshot now CONTROLS what
``rebar.config.tracker_dir`` / ``tickets_branch`` / ``tickets_remote`` — and
therefore every ``_store/*`` consumer — resolve to for the DURATION of a bound
operation:

AC1: a CLI operation and a command-write (``append_event``) each compose exactly
ONE snapshot, reused (never recomposed) by nested seams within the same operation.

AC2: the tracker dir/branch/remote resolved through a bound operation are FROZEN
against a later env/project-file mutation for the REST of that operation, while a
fresh operation observes the change — the store-level counterpart of the existing
AC2 snapshot-content freeze.

AC3: the ``REBAR_TRACKER_DIR`` env override and an explicit, different-repo
``root=`` argument both continue to outrank/bypass the bound snapshot.

Exclusion (documented, not a gap): push mode (``config.resolve_push_mode`` /
``_store/push.py``) is deliberately NOT bound — ``_io/import_ndjson.py`` toggles
``REBAR_SYNC_PUSH`` mid-bulk-import and depends on a LIVE per-call read
(``tests/unit/test_c3b_push_defer_coupling_heldout.py``). A test here proves that
carve-out holds even from inside a bound operation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import rebar.config as cfg
from rebar._operation_config import (
    active_snapshot,
    compose_and_bind_operation_snapshot,
)


@pytest.fixture(autouse=True)
def _clean_config_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    for key in list(__import__("os").environ):
        if key.startswith("REBAR_"):
            monkeypatch.delenv(key, raising=False)
    for var in ("REBAR_CONFIG", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    sandbox = tmp_path_factory.mktemp("rebar_root_sandbox")
    (sandbox / ".git").mkdir()
    monkeypatch.setenv("REBAR_ROOT", str(sandbox))
    cfg.reset_config_cache()


def _proj(
    tmp: Path,
    *,
    tracker_dir: str = ".tickets-tracker",
    branch: str = "tickets",
    remote: str = "origin",
) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / ".git").mkdir()
    (tmp / "rebar.toml").write_text(
        f"[tracker]\ndir = '{tracker_dir}'\nbranch = '{branch}'\n[sync]\nremote = '{remote}'\n",
        encoding="utf-8",
    )
    return tmp


# ── AC1: compose exactly once per operation, reused by nested seams ───────────
def test_nested_compose_and_bind_reuses_the_outer_snapshot(tmp_path: Path) -> None:
    p = _proj(tmp_path, tracker_dir="outer-store")
    calls: list[object] = []
    import rebar._operation_config as opcfg

    real = opcfg.compose_operation_snapshot

    def _record(**kwargs):
        snap = real(**kwargs)
        calls.append(snap)
        return snap

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(opcfg, "compose_operation_snapshot", _record)
        with compose_and_bind_operation_snapshot(repo_root=str(p)) as outer:
            with compose_and_bind_operation_snapshot(repo_root=str(p)) as inner:
                assert inner is outer  # reused, not recomposed
    assert len(calls) == 1, "a nested call must not recompose a second snapshot"


def test_no_binding_outside_any_operation(tmp_path: Path) -> None:
    assert active_snapshot() is None


# ── AC2: tracker dir/branch/remote are frozen for a bound operation ───────────
def test_tracker_dir_frozen_within_bound_operation(tmp_path: Path) -> None:
    p = _proj(tmp_path, tracker_dir="frozen-store")
    with compose_and_bind_operation_snapshot(repo_root=str(p)):
        before = cfg.tracker_dir(str(p))
        assert before == p / "frozen-store"

        # mutate the ambient config file AFTER the operation started
        (p / "rebar.toml").write_text(
            "[tracker]\ndir = 'mutated-store'\nbranch = 'tickets'\n[sync]\nremote = 'origin'\n",
            encoding="utf-8",
        )
        cfg.reset_config_cache()

        # still the value captured when this operation's snapshot was composed
        assert cfg.tracker_dir(str(p)) == p / "frozen-store"

    # the operation has ended: a fresh call observes the mutation
    assert cfg.tracker_dir(str(p)) == p / "mutated-store"


def test_tickets_branch_and_remote_frozen_within_bound_operation(tmp_path: Path) -> None:
    p = _proj(tmp_path, branch="branch-a", remote="remote-a")
    with compose_and_bind_operation_snapshot(repo_root=str(p)):
        assert cfg.tickets_branch(str(p)) == "branch-a"
        assert cfg.tickets_remote(str(p)) == "remote-a"

        (p / "rebar.toml").write_text(
            "[tracker]\ndir = '.tickets-tracker'\nbranch = 'branch-b'\n"
            "[sync]\nremote = 'remote-b'\n",
            encoding="utf-8",
        )
        cfg.reset_config_cache()

        assert cfg.tickets_branch(str(p)) == "branch-a"
        assert cfg.tickets_remote(str(p)) == "remote-a"

    assert cfg.tickets_branch(str(p)) == "branch-b"
    assert cfg.tickets_remote(str(p)) == "remote-b"


# ── AC3: explicit overrides still outrank / bypass the bound snapshot ─────────
def test_tracker_dir_env_override_still_wins_over_bound_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _proj(tmp_path, tracker_dir="from-config")
    monkeypatch.setenv("REBAR_TRACKER_DIR", "/explicit/env/override")
    with compose_and_bind_operation_snapshot(repo_root=str(p)):
        assert cfg.tracker_dir(str(p)) == Path("/explicit/env/override")


def test_different_repo_root_bypasses_the_bound_snapshot(tmp_path: Path) -> None:
    bound_repo = _proj(tmp_path / "bound", tracker_dir="bound-store")
    other_repo = _proj(tmp_path / "other", tracker_dir="other-store")
    with compose_and_bind_operation_snapshot(repo_root=str(bound_repo)):
        # a call for a DIFFERENT, explicit repo root is not served by the binding
        assert cfg.tracker_dir(str(other_repo)) == other_repo / "other-store"
        # the call for the bound repo's own root is still served by the binding
        assert cfg.tracker_dir(str(bound_repo)) == bound_repo / "bound-store"


# ── Documented exclusion: push mode stays live even inside a bound operation ──
def test_push_mode_stays_live_inside_a_bound_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\npush = 'always'\n", encoding="utf-8")
    cfg.reset_config_cache()
    with compose_and_bind_operation_snapshot(repo_root=str(p)):
        assert cfg.resolve_push_mode(str(p)) == "always"
        monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
        # push mode is intentionally NOT frozen by the binding (AC2 exclusion,
        # documented in this ticket's completion evidence): a mid-operation
        # environment toggle (exactly what _io/import_ndjson.py performs to defer
        # per-event pushes during a bulk import) is observed immediately.
        assert cfg.resolve_push_mode(str(p)) == "off"


# ── E2E: a real CLI write binds one snapshot for the whole invocation ────────
def test_cli_ticket_create_binds_one_snapshot_for_the_whole_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    import subprocess

    import rebar
    import rebar._operation_config as opcfg

    p = tmp_path / "repo"
    p.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=p, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(p))
    cfg.reset_config_cache()
    rebar.init_repo(repo_root=str(p))

    from rebar._cli import main

    real = opcfg.compose_operation_snapshot
    calls: list[object] = []

    def _record(**kwargs):
        snap = real(**kwargs)
        calls.append(snap)
        return snap

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(opcfg, "compose_operation_snapshot", _record)
        rc = main(["create", "task", "a title", "--output", "json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["id"]
    # the CLI dispatch composed ONE snapshot for the whole invocation; the nested
    # command-write seam (append_event) reused it rather than recomposing.
    assert len(calls) == 1


def test_tracker_dir_root_is_frozen_too_not_just_the_configured_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2 freezes the tracker directory as a WHOLE path, not merely the configured
    relative name: if ``REBAR_ROOT`` (the ambient repository-root selector) changes
    mid-operation, ``tracker_dir(root=None)`` must still resolve against the bound
    operation's OWN repository root, not a freshly re-resolved (and now different) one.
    Without this, a frozen relative name gets joined against a moved root — a partial,
    silently-broken freeze that only ``test_tracker_dir_frozen_within_bound_operation``
    (same root throughout) cannot catch."""
    original = _proj(tmp_path / "original", tracker_dir="orig-store")
    moved = _proj(tmp_path / "moved", tracker_dir="moved-store")
    monkeypatch.setenv("REBAR_ROOT", str(original))
    cfg.reset_config_cache()
    with compose_and_bind_operation_snapshot():
        assert cfg.tracker_dir() == original / "orig-store"
        monkeypatch.setenv("REBAR_ROOT", str(moved))
        # the bound operation's tracker dir must stay anchored to ITS OWN root
        assert cfg.tracker_dir() == original / "orig-store"
    # outside the operation (unbound), a fresh call follows the moved root
    assert cfg.tracker_dir() == moved / "moved-store"


# ── MCP tool-registrar proxy: a real registered tool actually binds a snapshot ──
def test_mcp_tool_registered_through_the_proxy_binds_a_snapshot(tmp_path: Path) -> None:
    """``bind_operation_snapshot_for_tools`` must not merely COMPILE — a tool registered
    through the returned proxy must observably run with an ``OperationSnapshot`` bound,
    and the binding must be gone once the call returns (no cross-call leakage)."""
    from rebar._operation_config import bind_operation_snapshot_for_tools

    p = _proj(tmp_path, tracker_dir="mcp-tool-store")

    class _FakeMCP:
        def tool(self, *_a, **_k):
            def _decorate(fn):
                return fn

            return _decorate

    proxy = bind_operation_snapshot_for_tools(_FakeMCP())

    seen: dict[str, object] = {}

    @proxy.tool(annotations={})
    def handler() -> str:
        snap = active_snapshot()
        seen["snapshot"] = snap
        seen["tracker_dir"] = cfg.tracker_dir(str(p)) if snap is not None else None
        return "ok"

    assert active_snapshot() is None  # nothing bound before the call
    result = handler()
    assert result == "ok"
    assert seen["snapshot"] is not None  # the tool body observed a bound snapshot
    assert seen["tracker_dir"] == p / "mcp-tool-store"
    assert active_snapshot() is None  # unbound again after the call returns


def test_mcp_async_tool_registered_through_the_proxy_binds_a_snapshot() -> None:
    """The async branch (``inspect.iscoroutinefunction`` -> ``awrapper``) must bind a
    snapshot too, and preserve the awaited return value — the async counterpart of
    ``test_mcp_tool_registered_through_the_proxy_binds_a_snapshot``, mirroring
    ``test_double_advisory_proxy_scopes_suppression_for_async_handler`` in
    ``test_cross_session_lib.py`` for the sibling proxy this wrapper mirrors."""
    import asyncio

    from rebar._operation_config import bind_operation_snapshot_for_tools

    class _FakeMCP:
        def tool(self, *_a, **_k):
            def _decorate(fn):
                return fn

            return _decorate

    proxy = bind_operation_snapshot_for_tools(_FakeMCP())

    seen: dict[str, object] = {}

    @proxy.tool(annotations={})
    async def handler() -> str:
        seen["snapshot"] = active_snapshot()
        return "ok"

    assert active_snapshot() is None
    result = asyncio.run(handler())
    assert result == "ok"
    assert seen["snapshot"] is not None
    assert active_snapshot() is None


def test_bridge_setup_reset_still_succeeds_when_compose_and_bind_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-open litmus: the CLI's ``compose_and_bind_operation_snapshot`` swallowing
    a real composition failure must never block a legacy operation that does not
    itself need a valid config (mirrors
    ``tests/unit/test_jira_onboard.py::test_reset_clears_and_exits`` at the CLI
    binding seam this ticket adds). A malformed ``[verify]`` value makes
    ``compose_operation_snapshot`` raise ``ConfigError`` — proven directly below —
    yet the reset command still runs to completion."""
    p = tmp_path
    (p / ".git").mkdir()
    (p / "rebar.toml").write_text(
        "[verify]\nmax_ticket_description_chars = 'not-an-int'\n", encoding="utf-8"
    )
    monkeypatch.setenv("REBAR_ROOT", str(p))
    cfg.reset_config_cache()

    from rebar._operation_config import compose_operation_snapshot

    with pytest.raises(cfg.ConfigError):
        compose_operation_snapshot(repo_root=str(p))

    from rebar._cli import main

    rc = main(["bridge", "setup", "--reset", "--yes"])
    assert rc == 0
    assert active_snapshot() is None  # no operation left a stale binding behind
