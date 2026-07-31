"""Task 77e1: every network-capable git call site is bounded by an explicit timeout.

Bug 747f proved this class is not theoretical — an unbounded git call on the snapshot
materialize path produced 40 sequential per-object fetch RPCs and a measured ~2.1-hour
hang — and fixed it by bounding ``src/rebar/_snapshot/repo_snapshot.py`` with
``_GIT_TIMEOUT = 300``, joining the older ``_store/push.py`` / ``_store/sync.py``
precedent. 77e1 closes the rest of the class in the two modules that were still
unbounded:

* ``rebar.review_bot.gerrit_client.GerritClient.clone_change_ref`` — five
  ``subprocess.run`` calls (``init``, ``fetch --depth 2``, ``checkout``,
  ``remote add``, ``fetch --depth 1``), none of which passed a timeout.
* ``rebar.opcert_service.workspace._git`` — the module's single git seam, which every
  call (including two unshallow network ``fetch``es) funnels through.

A timeout is only half the fix: an unbounded hang that becomes a bare
``subprocess.TimeoutExpired`` merely moves the mystery. ``TimeoutExpired`` is neither an
``OSError`` nor a ``CalledProcessError``, so it fell OUTSIDE both modules' existing
``except`` tuples — in ``gerrit_client`` that also bypassed the token-redaction handler,
and ``TimeoutExpired.cmd`` carries the ``user:token@host`` fetch URL. So these tests
assert the failure path too: the module's own descriptive error type, naming the git
operation and the bound, with the bot token redacted.

Everything here is offline — the git call is monkeypatched to raise ``TimeoutExpired``
without ever spawning a process.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from rebar.opcert_service.config import OpcertServiceConfig
from rebar.opcert_service.workspace import WorkspaceError
from rebar.review_bot.config import ReceiverConfig
from rebar.review_bot.gerrit_client import GerritError

_TOKEN = "s3cr3t-bot-token"


def _receiver_cfg(tmp_path) -> ReceiverConfig:
    return ReceiverConfig(
        llm_review_max_value=1,
        llm_review_block_value=-1,
        dedup_db_path=str(tmp_path / "voted.db"),
        gerrit_bot_token=_TOKEN,
        webhook_token="tok",
        project="rebar",
    )


def _client(tmp_path):
    from rebar.review_bot.gerrit_client import GerritClient

    return GerritClient(_receiver_cfg(tmp_path))


# ── the constants exist and are positive (mirrors test_snapshot_blobless_747f.py) ──


@pytest.mark.parametrize(
    "module_path",
    ["rebar.review_bot.gerrit_client", "rebar.opcert_service.workspace"],
)
def test_module_declares_a_positive_git_timeout(module_path):
    """Each module bounds its git calls with a module-level ``_GIT_TIMEOUT``.

    Same assertion shape as 747f's ``test_snapshot_blobless_747f.py`` — the constant is
    the shared mechanism, deliberately reused rather than reinvented per module.
    """
    import importlib

    module = importlib.import_module(module_path)
    timeout = getattr(module, "_GIT_TIMEOUT", None)
    assert timeout is not None, f"{module_path} declares no _GIT_TIMEOUT"
    assert isinstance(timeout, (int, float))
    assert timeout > 0


# ── gerrit_client: every subprocess call is bounded ──────────────────────────


def test_clone_change_ref_passes_a_timeout_to_every_subprocess_call(tmp_path):
    """Not just the two fetches — EVERY git call in ``clone_change_ref`` is bounded.

    A hung credential helper can stall ``git init`` or ``checkout`` just as easily as a
    stuck remote stalls a fetch, so the bound is applied at the seam, not cherry-picked.
    """
    from rebar.review_bot import gerrit_client as gc_mod

    seen: list[dict] = []

    def _fake_run(cmd, **kwargs):
        seen.append({"cmd": cmd, "timeout": kwargs.get("timeout")})
        return subprocess.CompletedProcess(cmd, 0, "", "")

    gc = _client(tmp_path)
    # Scope the patch tightly: ``gerrit_client`` does a plain ``import subprocess``, so
    # patching its attribute patches the stdlib module GLOBALLY. A ``with`` block undoes
    # it before any fixture teardown (the repo-HEAD guard shells out to git itself).
    with mock.patch.object(gc_mod.subprocess, "run", _fake_run):
        gc.clone_change_ref(42, "refs/changes/42/42/1", str(tmp_path / "dest"))

    assert seen, "clone_change_ref made no subprocess calls — fixture is wrong"
    unbounded = [c["cmd"] for c in seen if c["timeout"] is None]
    assert not unbounded, f"git call(s) invoked with no timeout: {unbounded}"
    for call in seen:
        assert call["timeout"] > 0


def test_clone_change_ref_timeout_raises_a_descriptive_gerrit_error(tmp_path):
    """A timeout becomes an actionable ``GerritError``, never a bare ``TimeoutExpired``.

    The message must name the git operation that timed out and the bound it exceeded —
    a timeout that surfaces as a cryptic exception just relocates the hang's mystery.
    """
    from rebar.review_bot import gerrit_client as gc_mod

    def _fake_run(cmd, **kwargs):
        # Fail on the network fetch, the call 747f's hang was about.
        if "fetch" in cmd:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 300))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    gc = _client(tmp_path)
    with mock.patch.object(gc_mod.subprocess, "run", _fake_run):
        with pytest.raises(GerritError) as excinfo:
            gc.clone_change_ref(42, "refs/changes/42/42/1", str(tmp_path / "dest"))

    message = str(excinfo.value)
    assert "timed out" in message.lower(), message
    assert "fetch" in message.lower(), f"error does not name the git operation: {message}"
    assert "300" in message, f"error does not name the bound it exceeded: {message}"


def test_clone_change_ref_timeout_does_not_leak_the_bot_token(tmp_path):
    """``TimeoutExpired.cmd`` carries the ``user:token@host`` fetch URL.

    The pre-existing handler redacted only ``stderr``, and ``TimeoutExpired`` did not even
    reach it. Route the timeout through the SAME redaction so a bounded failure is not a
    credential-disclosure path.
    """
    from rebar.review_bot import gerrit_client as gc_mod

    def _fake_run(cmd, **kwargs):
        if "fetch" in cmd:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 300))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    gc = _client(tmp_path)
    with mock.patch.object(gc_mod.subprocess, "run", _fake_run):
        with pytest.raises(GerritError) as excinfo:
            gc.clone_change_ref(42, "refs/changes/42/42/1", str(tmp_path / "dest"))

    message = str(excinfo.value)
    assert _TOKEN not in message, "the bot token leaked into the timeout error"
    import urllib.parse

    assert urllib.parse.quote(_TOKEN, safe="") not in message, (
        "the percent-encoded bot token leaked into the timeout error"
    )


# ── opcert workspace: the single git seam is bounded ─────────────────────────


def test_workspace_git_passes_a_timeout(tmp_path):
    """``workspace._git`` is the module's only git seam, so bounding it bounds them all."""
    from rebar.opcert_service import workspace as ws_mod

    seen: list[dict] = []

    def _fake_run(cmd, **kwargs):
        seen.append({"cmd": cmd, "timeout": kwargs.get("timeout")})
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with mock.patch.object(ws_mod.subprocess, "run", _fake_run):
        ws_mod._git(str(tmp_path), "fetch", "--quiet", "review", "main")

    assert len(seen) == 1
    assert seen[0]["timeout"] is not None, "workspace._git invoked git with no timeout"
    assert seen[0]["timeout"] > 0


def test_workspace_git_timeout_raises_a_descriptive_workspace_error(tmp_path):
    """A timeout becomes an actionable ``WorkspaceError``, never a bare ``TimeoutExpired``.

    ``_git_ok`` converts a non-zero exit into ``WorkspaceError``, but a ``TimeoutExpired``
    raised out of ``subprocess.run`` skipped that conversion entirely and escaped as an
    unhandled stdlib exception carrying no context about which fetch stalled.
    """
    from rebar.opcert_service import workspace as ws_mod

    def _fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 300))

    with mock.patch.object(ws_mod.subprocess, "run", _fake_run):
        with pytest.raises(WorkspaceError) as excinfo:
            ws_mod._git(str(tmp_path), "fetch", "--quiet", "review", "main")

    message = str(excinfo.value)
    assert "timed out" in message.lower(), message
    assert "fetch" in message.lower(), f"error does not name the git operation: {message}"
    assert "300" in message, f"error does not name the bound it exceeded: {message}"


def test_prepare_workspace_surfaces_a_fetch_timeout_as_workspace_error(tmp_path):
    """End-to-end through the real entry point: a stuck remote fails bounded, not hung.

    This is the user-visible shape of the 747f class in the opcert service — an
    ephemeral gate job whose authoritative-state fetch never returns.
    """
    from rebar.opcert_service import workspace as ws_mod

    def _fake_run(cmd, **kwargs):
        if "fetch" in cmd:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 300))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    cfg = OpcertServiceConfig(
        review_remote_url="https://example.invalid/rebar.git",
        tickets_remote_url="https://example.invalid/rebar-tickets.git",
    )
    with mock.patch.object(ws_mod.subprocess, "run", _fake_run):
        with pytest.raises(WorkspaceError) as excinfo:
            ws_mod.prepare_workspace(cfg)

    assert "timed out" in str(excinfo.value).lower()
