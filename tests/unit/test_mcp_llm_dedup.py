"""Tool-level proof that the MCP gate tools de-duplicate concurrent in-flight calls (bug d80d).

``test_mcp_inflight`` pins the singleflight registry in isolation; this pins the WIRING —
that ``review_plan`` / ``verify_completion``, as registered on the FastMCP server and run on
the server's worker threads (``offload_sync_tools``), actually route through the registry so
two concurrent same-ticket calls invoke the underlying billable ``rebar.llm`` gate EXACTLY
ONCE (AC #2). The gate is monkeypatched with a fake that blocks on a ``threading.Event`` and
counts calls, so the verdict is a call count — deterministic, zero tokens.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import anyio
import pytest

import rebar
from rebar import _mcp_inflight as inflight
from rebar._mcp_health import offload_sync_tools
from rebar._mcp_llm import register_llm_tools


def _server_with_llm_tools(readonly: bool = False):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import Settings

    Settings.model_rebuild()
    mcp = FastMCP("dedup-test")
    ctx = SimpleNamespace(allow_llm=lambda: True, readonly=lambda: readonly)
    register_llm_tools(mcp, ctx)
    # The real server offloads sync tool bodies onto worker threads; do the same so two
    # concurrent CallTool requests genuinely overlap (a sync body on the loop cannot).
    offload_sync_tools(mcp)
    return mcp


class _BlockingReviewPlan:
    def __init__(self):
        self.calls = 0
        self._started = threading.Event()
        self._release = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, ticket_id, **kwargs):
        with self._lock:
            self.calls += 1
        self._started.set()
        self._release.wait(timeout=5)
        return {"verdict": "PASS", "ticket_id": ticket_id}


@pytest.mark.parametrize("tool_name", ["review_plan", "verify_completion"])
def test_two_concurrent_calls_invoke_the_gate_exactly_once(tool_name, monkeypatch):
    inflight.reset_registry()
    fake = _BlockingReviewPlan()
    import rebar.llm

    monkeypatch.setattr(rebar.llm, tool_name, fake)
    # readonly=True keeps both gate bodies store-independent (review_plan skips signing;
    # verify_completion skips the COMPLETION_VERDICT sidecar write) so the test needs no
    # real store — it proves ONLY the singleflight wiring, deterministically + token-free.
    mcp = _server_with_llm_tools(readonly=True)

    # Release the leader only once a *real* follower has attached to its in-flight entry.
    # Spying on ``_await_follower`` (the follower-only branch of ``run_singleflight``) makes
    # the proof deterministic rather than reliant on a fixed sleep: the first CallTool pays a
    # cold pydantic/thread-pool warm-up that blocks the loop, so a fixed window is racy. If
    # de-dup were broken the follower would become a SECOND leader, ``_await_follower`` would
    # never run, and the bounded fallback below still releases so the test fails (calls == 2)
    # instead of hanging — the teeth stay intact.
    follower_attached = threading.Event()
    orig_await_follower = inflight._await_follower

    def _spy_await_follower(entry):
        follower_attached.set()
        return orig_await_follower(entry)

    monkeypatch.setattr(inflight, "_await_follower", _spy_await_follower)

    results: dict[str, object] = {}

    async def scenario():
        async def leader():
            results["a"] = await mcp._tool_manager.call_tool(tool_name, {"ticket_id": "d80d"})

        async def follower():
            # Issue the duplicate WHILE the leader is still blocked inside the gate.
            while not fake._started.is_set():  # noqa: ASYNC110 — set from a worker thread
                await anyio.sleep(0.005)
            results["b"] = await mcp._tool_manager.call_tool(tool_name, {"ticket_id": "d80d"})

        async def releaser():
            deadline = anyio.current_time() + 5.0
            while not follower_attached.is_set() and anyio.current_time() < deadline:  # noqa: ASYNC110
                await anyio.sleep(0.005)
            fake._release.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(leader)
            tg.start_soon(follower)
            tg.start_soon(releaser)

    anyio.run(scenario)

    assert fake.calls == 1, "two concurrent same-ticket calls must invoke the gate once"
    # Both callers received a PASS verdict (FastMCP returns a (content, structured) tuple).
    for key in ("a", "b"):
        assert results[key] is not None


# ── Material-sensitive dedup (bug 3b27-8f4b): an edit must not attach to a stale run ────────


@pytest.fixture
def store(tmp_path, monkeypatch):
    import subprocess

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
    monkeypatch.chdir(repo)
    rebar.init_repo(repo_root=str(repo))
    inflight.reset_registry()
    return repo


class _DescriptionRecordingReview:
    """A blocking ``rebar.llm.review_plan`` stand-in that records the description the
    store held when each gate call began — the material that call would review."""

    def __init__(self):
        self.seen: list[str] = []
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.first_started = threading.Event()

    @property
    def calls(self) -> int:
        with self._lock:
            return len(self.seen)

    def __call__(self, ticket_id, **kwargs):
        import rebar

        text = rebar.show_ticket(ticket_id)["description"]
        with self._lock:
            self.seen.append(text)
        self.first_started.set()
        self.release.wait(timeout=5)
        return {"verdict": "BLOCK", "ticket_id": ticket_id}


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_review_plan_start_after_an_edit_runs_a_fresh_review(store, monkeypatch):
    import rebar.llm

    tid = rebar.create_ticket("task", "stale review", description="## Plan\nORIGINAL\n")
    fake = _DescriptionRecordingReview()
    monkeypatch.setattr(rebar.llm, "review_plan", fake)
    mcp = _server_with_llm_tools()

    async def start():
        return await mcp._tool_manager.call_tool("review_plan_start", {"ticket_id": tid})

    first = anyio.run(start)
    assert fake.first_started.wait(timeout=5)
    rebar.edit_ticket(tid, description="## Plan\nEDITED\n")
    second = anyio.run(start)
    try:
        assert second["job_id"] != first["job_id"], (
            "a start issued after the edit attached to the run that read the old description"
        )
        assert _wait_until(lambda: fake.calls == 2)
        assert "EDITED" in fake.seen[1]
    finally:
        fake.release.set()


def test_sync_review_plan_after_an_edit_does_not_share_the_stale_result(store, monkeypatch):
    import rebar.llm

    tid = rebar.create_ticket("task", "stale sync review", description="## Plan\nORIGINAL\n")
    fake = _DescriptionRecordingReview()
    monkeypatch.setattr(rebar.llm, "review_plan", fake)
    # readonly keeps the gate body from signing; the dedup key still applies.
    mcp = _server_with_llm_tools(readonly=True)

    async def scenario():
        async def leader():
            await mcp._tool_manager.call_tool("review_plan", {"ticket_id": tid})

        async def after_edit():
            while not fake.first_started.is_set():  # noqa: ASYNC110 — set from a worker thread
                await anyio.sleep(0.005)
            await anyio.to_thread.run_sync(
                lambda: rebar.edit_ticket(tid, description="## Plan\nEDITED\n")
            )
            await mcp._tool_manager.call_tool("review_plan", {"ticket_id": tid})

        async def releaser():
            # Release once the post-edit call has entered the gate itself; if it attached to
            # the stale run instead, release after the bound so the test fails, not hangs.
            deadline = anyio.current_time() + 3.0
            while fake.calls < 2 and anyio.current_time() < deadline:  # noqa: ASYNC110
                await anyio.sleep(0.01)
            fake.release.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(leader)
            tg.start_soon(after_edit)
            tg.start_soon(releaser)

    anyio.run(scenario)

    assert fake.calls == 2, "the post-edit call shared the run that reviewed the old text"
    assert "EDITED" in fake.seen[1]


def test_plan_review_variant_tracks_material_and_degrades_to_a_none_key(monkeypatch):
    from rebar._mcp_llm import _plan_review_variant
    from rebar.llm.plan_review import attest

    monkeypatch.setattr(attest, "current_material_fingerprint", lambda _tid: "aaaa")
    before = _plan_review_variant("t", None)
    monkeypatch.setattr(attest, "current_material_fingerprint", lambda _tid: "bbbb")
    after = _plan_review_variant("t", None)
    monkeypatch.setattr(attest, "current_material_fingerprint", lambda _tid: None)
    unreadable = _plan_review_variant("t", None)

    assert before != after, "a material change must change the dedup variant"
    assert unreadable == "source=attested;material=None"
    assert unreadable not in {before, after}


# ── Material-sensitive completion dedup (bug c912-f274) ─────────────────────────────────────


def test_verify_completion_start_after_an_edit_runs_a_fresh_check(store, monkeypatch):
    import rebar.llm

    tid = rebar.create_ticket("task", "stale completion", description="## Plan\nORIGINAL\n")
    fake = _DescriptionRecordingReview()
    monkeypatch.setattr(rebar.llm, "verify_completion", fake)
    # readonly keeps the completion body from writing a verdict sidecar; the key still applies.
    mcp = _server_with_llm_tools(readonly=True)

    async def start():
        return await mcp._tool_manager.call_tool("verify_completion_start", {"ticket_id": tid})

    first = anyio.run(start)
    assert fake.first_started.wait(timeout=5)
    rebar.edit_ticket(tid, description="## Plan\nEDITED\n")
    second = anyio.run(start)
    try:
        assert second["job_id"] != first["job_id"], (
            "a completion start issued after the edit attached to the run that read the old text"
        )
        assert _wait_until(lambda: fake.calls == 2)
        assert "EDITED" in fake.seen[1]
    finally:
        fake.release.set()


def test_sync_verify_completion_after_an_edit_does_not_share_the_stale_result(store, monkeypatch):
    import rebar.llm

    tid = rebar.create_ticket("task", "stale sync check", description="## Plan\nORIGINAL\n")
    fake = _DescriptionRecordingReview()
    monkeypatch.setattr(rebar.llm, "verify_completion", fake)
    mcp = _server_with_llm_tools(readonly=True)

    async def scenario():
        async def leader():
            await mcp._tool_manager.call_tool("verify_completion", {"ticket_id": tid})

        async def after_edit():
            while not fake.first_started.is_set():  # noqa: ASYNC110 — set from a worker thread
                await anyio.sleep(0.005)
            await anyio.to_thread.run_sync(
                lambda: rebar.edit_ticket(tid, description="## Plan\nEDITED\n")
            )
            await mcp._tool_manager.call_tool("verify_completion", {"ticket_id": tid})

        async def releaser():
            deadline = anyio.current_time() + 3.0
            while fake.calls < 2 and anyio.current_time() < deadline:  # noqa: ASYNC110
                await anyio.sleep(0.01)
            fake.release.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(leader)
            tg.start_soon(after_edit)
            tg.start_soon(releaser)

    anyio.run(scenario)

    assert fake.calls == 2, "the post-edit call shared the run that checked the old text"
    assert "EDITED" in fake.seen[1]


def _raise_read_failure(_tid):
    raise RuntimeError("pinned ticket read failed")


@pytest.mark.parametrize(
    ("build", "prefix"),
    [
        (lambda m, tid: m._completion_variant(tid, None, None), "graph=None;source=attested"),
        (lambda m, tid: m._plan_review_variant(tid, None), "source=attested"),
    ],
    ids=["verify_completion", "plan_review"],
)
def test_gate_variant_tracks_material_and_degrades_to_a_none_key(build, prefix, monkeypatch):
    import rebar._mcp_llm as mcp_llm
    from rebar.llm.plan_review import attest

    monkeypatch.setattr(attest, "current_material_fingerprint", lambda _tid: "aaaa")
    before = build(mcp_llm, "t")
    monkeypatch.setattr(attest, "current_material_fingerprint", lambda _tid: "bbbb")
    after = build(mcp_llm, "t")
    monkeypatch.setattr(attest, "current_material_fingerprint", lambda _tid: None)
    unreadable = build(mcp_llm, "t")
    # A raising read (a pinned-view failure re-raised by the light read) must not fail the
    # tool call before the gate starts; it degrades to the same None key.
    monkeypatch.setattr(attest, "current_material_fingerprint", _raise_read_failure)
    raising = build(mcp_llm, "t")

    assert before != after, "a material change must change the dedup variant"
    assert unreadable == f"{prefix};material=None"
    assert raising == unreadable
    assert unreadable not in {before, after}
