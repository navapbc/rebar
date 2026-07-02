"""The four-pass code-review gate must run its agentic passes INSIDE a snapshot gate
session rooted at the REVIEWED code (``repo_root``) — the raze-vet-ditch safeguard.

Regression (this suite pins the fix): the WS4 single-pass -> four-pass swap
(``produce_code_review_verdict``) dropped the ``gate_read_root`` wrapping that the retired
single-pass ``review_code`` had (``operations.py``). So every REAL review (the production
``PydanticAIRunner``) fail-closed at the first agentic step ('base') with ``assert_gated``
("agentic filesystem tools ... OUTSIDE the repo-snapshot gate process"), casting a
``coverage-gap (llm-unavailable)`` BLOCK on every change. The existing WS4 tests only used
``FakeRunner`` — which never calls ``assert_gated`` — so they never caught it.

Two properties are pinned here, independent of the runner:
1. the workflow run happens inside a gate session (``in_gate_session()`` True), and
2. the agent step's file-tool root is the reviewed ``repo_root`` (the voter's isolated
   patchset clone), NOT the configured attested ``origin/main`` — because "the code being
   reviewed may not be rebar code itself; it is the host/client project's code".
"""

from __future__ import annotations

import subprocess

import pytest

from rebar.llm import config as llmcfg
from rebar.llm.config import LLMConfig
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow import gate_dispatch

pytestmark = pytest.mark.unit

_DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+print('hi')\n"


def test_code_review_gate_runs_inside_gate_session_rooted_at_repo_root(tmp_path, monkeypatch):
    monkeypatch.delenv("REBAR_GATE_ALLOW_UNGATED", raising=False)
    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda repo_root=None: True)
    # The WS5 security detector is its own concern; keep this focused on the gate wrapping.
    from rebar.llm.code_review import detectors as _det

    monkeypatch.setattr(_det, "run_security_detectors", lambda **kw: {})

    # Stand in for the voter's isolated patchset clone (voter.py clones the reviewed change
    # into a fresh tempdir). A real git repo so LLMConfig.from_env resolves repo_path to it.
    repo = tmp_path / "reviewed-clone"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)

    seen: dict = {}

    def _spy(doc, inputs, **kw):
        # Record the gate state + the agent step's file-tool root AT THE MOMENT the workflow
        # would run its (agentic) steps.
        seen["gated"] = llmcfg.in_gate_session()
        agent_runner = kw.get("agent_runner")
        cfg = getattr(agent_runner, "_config", None)
        seen["root"] = getattr(cfg, "repo_path", None)

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
        LLMConfig.from_env(repo_root=str(repo)),
        diff_text=_DIFF,
        changed_files=["x.py"],
        runner=FakeRunner(structured={}),
        repo_root=str(repo),
        enabled=True,
    )

    assert seen.get("gated") is True, (
        "code-review gate must run its agentic passes INSIDE a snapshot gate session — "
        "assert_gated fail-closes the 'base' step otherwise (coverage-gap veto on every change)"
    )
    assert seen.get("root") == str(repo), (
        "the agent's file tools must be rooted at the REVIEWED code (repo_root, the isolated "
        f"patchset clone), not origin/main or the server checkout; got {seen.get('root')!r}"
    )
