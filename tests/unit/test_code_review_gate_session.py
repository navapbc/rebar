"""The four-pass code-review gate must run its agentic passes inside the SAME snapshot-gate
session pattern as every other code-reading gate (operations.review_ticket / review_plan /
verify_completion): ``resolve_gate_handle -> apply_handle -> gate_read_root``, in ATTESTED
mode, so the agent gets BOTH a pinned code snapshot AND a pinned clone of the ticket store.

The ticket-store clone is the load-bearing REQUIREMENT (epic raze-vet-ditch): the reviewed
project's tickets live on the orphan ``tickets`` branch, ABSENT from any code checkout, so
without it the agent's rebar ticket tools error on a missing ``.tickets-tracker`` and cannot
use the ticket system. Two regressions this pins: (1) the WS4 single->four-pass swap
(``produce_code_review_verdict``) dropped the ``gate_read_root`` wrapping the retired
single-pass ``review_code`` had, so a real ``PydanticAIRunner`` review fail-closed at the
'base' step; (2) a first remediation used LOCAL mode — which marks the session but does NOT
materialize the ticket clone — so the agent still had no ticket access. The gate must run
ATTESTED (like plan review), with both the code root AND the ticket root active.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar.llm import config as llmcfg
from rebar.llm.config import LLMConfig, current_code_root, current_tickets_root
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow import gate_dispatch

pytestmark = pytest.mark.unit

_DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+print('hi')\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo_with_origin(tmp_path, monkeypatch):
    """A rebar repo with an ``origin`` remote: a code commit on ``main`` pushed to origin, and a
    rebar ticket (auto-pushed to ``origin/tickets``) — so the attested gate can materialize both
    the code snapshot and the ticket-store clone. Mirrors test_gate_ticket_snapshot.py."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    (repo / "x.py").write_text("print('hi')\n")
    _git(repo, "add", "x.py")
    _git(repo, "commit", "-q", "-m", "content")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main")
    tid = rebar.create_ticket("task", "code-review gate ticket-access test", repo_root=str(repo))
    return repo, tid


def test_code_review_gate_runs_attested_with_code_and_ticket_roots(
    repo_with_origin, tmp_path, monkeypatch
):
    repo, tid = repo_with_origin
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(tmp_path / "gate-store"))
    monkeypatch.delenv("REBAR_GATE_ALLOW_UNGATED", raising=False)
    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda repo_root=None: True)
    # The WS5 security detector is its own concern; keep this focused on the gate wrapping.
    from rebar.llm.code_review import detectors as _det

    monkeypatch.setattr(_det, "run_security_detectors", lambda **kw: {})

    seen: dict = {}

    def _spy(doc, inputs, **kw):
        # Capture the gate state AT THE MOMENT the (agentic) passes would run.
        seen["gated"] = llmcfg.in_gate_session()
        seen["code_root"] = current_code_root()
        seen["tickets_root"] = current_tickets_root()
        cfg = getattr(kw.get("agent_runner"), "_config", None)
        seen["cfg_tickets"] = getattr(cfg, "tickets_path", None)

        class _R:
            run_id = "r"
            workflow_name = doc.get("name")
            status = "succeeded"
            terminal_step = None
            terminal_output = {"verdict": "PASS", "blocking": [], "advisory": [], "coverage": {}}
            outputs: dict = {}
            steps: dict = {}
            error = None

        return _R()

    from rebar.llm.workflow import executor as _executor

    monkeypatch.setattr(_executor, "run_workflow", _spy)

    gate_dispatch.produce_code_review_verdict(
        gate_dispatch.CodeReviewRequest(
            LLMConfig.from_env(repo_root=str(repo)),
            head="HEAD",
            source="attested",
            diff_text=_DIFF,
            changed_files=["x.py"],
            runner=FakeRunner(structured={}),
            repo_root=str(repo),
            enabled=True,
        )
    )

    assert seen.get("gated") is True, (
        "code-review gate must run its agentic passes INSIDE a snapshot gate session"
    )
    assert seen.get("code_root"), (
        "attested: the pinned code snapshot must be the active code root (not the mutable checkout)"
    )
    assert seen.get("tickets_root"), (
        "attested: the pinned ticket-store clone MUST be active — the agent's rebar ticket tools "
        "read it; without it they error on a missing .tickets-tracker (ticket-access requirement)"
    )
    # The materialized ticket store actually holds the ticket — the agent can read it, no
    # "cannot list '<clone>/.tickets-tracker'" error.
    tracker = Path(seen["tickets_root"]) / ".tickets-tracker"
    assert tracker.is_dir()
    short = tid.split("-")[0]
    assert any(d.is_dir() and d.name.startswith(short) for d in tracker.iterdir()), (
        f"the pinned ticket store has no event dir for {tid!r}"
    )
    assert seen.get("cfg_tickets") == seen.get("tickets_root"), (
        "the config handed to the agent runner must be re-rooted onto the ticket clone"
    )


def test_code_review_gate_runner_is_rebuilt_from_rerooted_ticket_store(
    repo_with_origin, tmp_path, monkeypatch
):
    """RED for bug pelt-mead-aeon: the runner that EXECUTES the passes must carry the
    RE-ROOTED cfg (``tickets_path`` = materialized store), not the pre-snapshot cfg.

    ``produce_code_review_verdict`` built ``runner_sel`` from cfg BEFORE ``apply_handle``
    re-rooted it, then handed that stale runner to the passes. ``get_runner(cfg,
    override=runner_sel)`` returns the override verbatim and ``PydanticAIRunner.run`` reads
    its OWN baked-in ``self._config`` — so the agent's ``rebar_tools`` ran against the bare
    clone (``tickets_path`` None → falls back to ``repo_path``) and errored on a missing
    ``.tickets-tracker``, casting no vote. This asserts on the RUNNER actually used
    (``agent_runner._runner._config``), NOT the ``RunnerAgentStep.config`` (which the sibling
    test already covers), and takes the PRODUCTION path (``runner=None``) so the rebuild is
    actually exercised — an injected runner would mask it.
    """
    repo, tid = repo_with_origin
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(tmp_path / "gate-store"))
    monkeypatch.delenv("REBAR_GATE_ALLOW_UNGATED", raising=False)
    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda repo_root=None: True)
    from rebar.llm.code_review import detectors as _det

    monkeypatch.setattr(_det, "run_security_detectors", lambda **kw: {})

    class _RecordingRunner:
        """A runner whose ``_config`` is exactly the cfg it was built from — so the test can see
        WHICH cfg (pre-snapshot vs re-rooted) the effective runner carries."""

        name = "recording"

        def __init__(self, config):
            self._config = config

        def preflight(self):
            return None

    # Replace get_runner at its source (the gate imports it locally): the production path builds
    # the runner via get_runner(cfg), so each call captures the cfg it was built from.
    from rebar.llm import runner as _runner_mod

    monkeypatch.setattr(_runner_mod, "get_runner", lambda cfg, **kw: _RecordingRunner(cfg))

    seen: dict = {}

    def _spy(doc, inputs, **kw):
        agent_runner = kw.get("agent_runner")
        runner = getattr(agent_runner, "_runner", None)
        seen["runner_tickets"] = getattr(getattr(runner, "_config", None), "tickets_path", None)
        seen["tickets_root"] = current_tickets_root()

        class _R:
            run_id = "r"
            workflow_name = doc.get("name")
            status = "succeeded"
            terminal_step = None
            terminal_output = {"verdict": "PASS", "blocking": [], "advisory": [], "coverage": {}}
            outputs: dict = {}
            steps: dict = {}
            error = None

        return _R()

    from rebar.llm.workflow import executor as _executor

    monkeypatch.setattr(_executor, "run_workflow", _spy)

    gate_dispatch.produce_code_review_verdict(
        gate_dispatch.CodeReviewRequest(
            LLMConfig.from_env(repo_root=str(repo)),
            head="HEAD",
            source="attested",
            diff_text=_DIFF,
            changed_files=["x.py"],
            runner=None,  # PRODUCTION path — the gate must build+rebuild the runner itself
            repo_root=str(repo),
            enabled=True,
        )
    )

    assert seen.get("tickets_root"), "attested gate must materialize a ticket-store clone"
    assert seen.get("runner_tickets") == seen.get("tickets_root"), (
        "the runner that EXECUTES the passes must carry the re-rooted ticket store "
        f"(got tickets_path={seen.get('runner_tickets')!r}, "
        f"expected {seen.get('tickets_root')!r}); otherwise the agent's rebar tools run "
        "against the bare clone and error on a missing .tickets-tracker"
    )


# ── local session persistence (story paradoxal-balsamic-bubblefish) ───────────────────────────
def _stub_workflow(monkeypatch, verdict: dict) -> None:
    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda repo_root=None: True)
    from rebar.llm.code_review import detectors as _det

    monkeypatch.setattr(_det, "run_security_detectors", lambda **kw: {})

    def _spy(doc, inputs, **kw):
        class _R:
            run_id = "r"
            workflow_name = doc.get("name")
            status = "succeeded"
            terminal_step = None
            terminal_output = verdict
            outputs: dict = {}
            steps: dict = {}
            error = None

        return _R()

    from rebar.llm.workflow import executor as _executor

    monkeypatch.setattr(_executor, "run_workflow", _spy)


def test_local_session_review_creates_reuses_artifact_with_session_and_deps(
    repo_with_origin, monkeypatch
):
    """A local review keyed by ``session_id`` emits a ``code-review: session:<id>`` artifact whose
    payload carries the ``session_id`` + the reviewed-file ``deps`` map; a SECOND review under the
    same session REUSES that artifact (append, not duplicate) — the convergence memory."""
    from rebar.llm.code_review import sidecar

    repo, _tid = repo_with_origin
    verdict = {
        "verdict": "PASS",
        "blocking": [],
        "advisory": [{"finding": "nit", "location": "x.py:1"}],
        "coverage": {},
    }
    _stub_workflow(monkeypatch, verdict)

    def _run():
        return gate_dispatch.produce_code_review_verdict(
            gate_dispatch.CodeReviewRequest(
                LLMConfig.from_env(repo_root=str(repo)),
                head="HEAD",
                diff_text=_DIFF,
                changed_files=["x.py"],
                runner=FakeRunner(structured={}),
                session_id="sess-abc",
                repo_root=str(repo),
                enabled=True,
            )
        )

    _run()
    arts = [
        t
        for t in rebar.list_tickets(ticket_type="code_review", repo_root=str(repo)) or []
        if str(t.get("title") or "") == "code-review: session:sess-abc"
    ]
    assert len(arts) == 1, "one session-keyed artifact created"
    got = sidecar.latest_code_review_result("session:sess-abc", repo_root=str(repo))
    assert got is not None
    assert got["session_id"] == "sess-abc"
    assert "x.py" in got["deps"] and got["deps"]["x.py"] != "absent"

    _run()  # second review, same session → reuse, not duplicate
    arts2 = [
        t
        for t in rebar.list_tickets(ticket_type="code_review", repo_root=str(repo)) or []
        if str(t.get("title") or "") == "code-review: session:sess-abc"
    ]
    assert len(arts2) == 1, "second review under the same session reuses the artifact"


def test_pass1_finder_receives_no_prior_findings(repo_with_origin, monkeypatch):
    """Neutrality invariant (ADR 0008 Invariant 1): prior SURFACED findings reach ONLY the
    region-gated floor's novelty seam — NEVER the workflow that runs the Pass-1 finder. We capture
    the workflow inputs during a produce run whose reader returns a distinctive prior finding, and
    assert that sentinel appears nowhere in the finder's inputs."""
    from rebar.llm.code_review import sidecar

    repo, _tid = repo_with_origin
    sentinel = "PRIOR_ONLY_SENTINEL_ZZZ"
    monkeypatch.setattr(
        sidecar,
        "latest_code_review_result",
        lambda key, repo_root=None: {
            "findings": [{"id": "P1", "finding": sentinel, "priority": 0.2, "location": "x.py"}],
            "deps": {"x.py": "hh"},
        },
    )
    captured: dict = {}

    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda repo_root=None: True)
    from rebar.llm.code_review import detectors as _det

    monkeypatch.setattr(_det, "run_security_detectors", lambda **kw: {})

    def _spy(doc, inputs, **kw):
        captured["inputs"] = inputs

        class _R:
            run_id = "r"
            workflow_name = doc.get("name")
            status = "succeeded"
            terminal_step = None
            terminal_output = {"verdict": "PASS", "blocking": [], "advisory": [], "coverage": {}}
            outputs: dict = {}
            steps: dict = {}
            error = None

        return _R()

    from rebar.llm.workflow import executor as _executor

    monkeypatch.setattr(_executor, "run_workflow", _spy)
    gate_dispatch.produce_code_review_verdict(
        gate_dispatch.CodeReviewRequest(
            LLMConfig.from_env(repo_root=str(repo)),
            head="HEAD",
            diff_text=_DIFF,
            changed_files=["x.py"],
            runner=FakeRunner(structured={}),
            session_id="sess-neutral",
            repo_root=str(repo),
            enabled=True,
        )
    )
    assert captured.get("inputs") is not None, "workflow must have run"
    assert sentinel not in json.dumps(captured["inputs"]), (
        "prior findings leaked into the Pass-1 finder inputs — the neutrality invariant is broken"
    )


def test_review_code_cli_mints_uuid_when_no_session(monkeypatch):
    """The CLI resolves the session key via ``resolve_session_id()``; when it returns None (bare
    invocation) a per-invocation uuid4 hex is minted (32 hex chars), and a set session var is
    passed through verbatim."""
    from rebar._cli import _llm_commands

    captured: dict = {}

    def _fake_review_code(**kw):
        captured.update(kw)
        return {"verdict": "PASS", "blocking": [], "advisory": [], "coverage": {}, "coaching": []}

    import rebar

    monkeypatch.setattr(rebar.llm, "review_code", _fake_review_code)
    # bare: no session var → uuid4 fallback
    monkeypatch.setattr("rebar._commands.session_id.resolve_session_id", lambda: None)
    _llm_commands._review_code(["-o", "json"])
    sid = captured.get("session_id")
    assert isinstance(sid, str) and len(sid) == 32 and all(c in "0123456789abcdef" for c in sid)
    # explicit session var → passed through
    captured.clear()
    monkeypatch.setattr("rebar._commands.session_id.resolve_session_id", lambda: "my-session")
    _llm_commands._review_code(["-o", "json"])
    assert captured.get("session_id") == "my-session"


def _boom(*a, **k):
    raise RuntimeError("simulated store failure")


def _review_with_session(repo, monkeypatch, *, session_id="sess-boom"):
    return gate_dispatch.produce_code_review_verdict(
        gate_dispatch.CodeReviewRequest(
            LLMConfig.from_env(repo_root=str(repo)),
            head="HEAD",
            diff_text=_DIFF,
            changed_files=["x.py"],
            runner=FakeRunner(structured={}),
            session_id=session_id,
            repo_root=str(repo),
            enabled=True,
        )
    )


def test_artifact_create_or_reuse_failure_never_fails_the_review(repo_with_origin, monkeypatch):
    """AC6 resilience: if the session-artifact LOOKUP/CREATE raises (store down), the local review
    still returns its verdict — the best-effort resolve/create must never fail the review."""
    repo, _tid = repo_with_origin
    _stub_workflow(monkeypatch, {"verdict": "PASS", "blocking": [], "advisory": [], "coverage": {}})
    monkeypatch.setattr(rebar, "list_tickets", _boom)
    monkeypatch.setattr(rebar, "create_ticket", _boom)
    result = _review_with_session(repo, monkeypatch)
    assert result.get("verdict") == "PASS", "artifact create/reuse failure must not fail the review"


def test_artifact_emit_failure_never_fails_the_review(repo_with_origin, monkeypatch):
    """AC6 resilience: even when the artifact resolves and the sidecar EMIT's underlying event
    append raises, the review still returns its verdict (the emit is best-effort)."""
    repo, _tid = repo_with_origin
    _stub_workflow(monkeypatch, {"verdict": "PASS", "blocking": [], "advisory": [], "coverage": {}})
    # An existing artifact resolves (so the emit path IS reached), then the event append blows up.
    monkeypatch.setattr(
        rebar,
        "list_tickets",
        lambda *a, **k: [{"ticket_id": "art-1", "title": "code-review: session:sess-boom"}],
    )
    from rebar._commands import _seam

    monkeypatch.setattr(_seam, "append_event", _boom)
    result = _review_with_session(repo, monkeypatch)
    assert result.get("verdict") == "PASS", "artifact emit failure must not fail the review"
