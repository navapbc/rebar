"""``review_code`` must WIRE its ``ref``/``source`` and STAMP the resulting provenance.

The shim used to declare ``ref``/``source`` and then drop both: neither reached the
``CodeReviewRequest``, and the returned ``review_result`` carried none of the
``source``/``verified_at_sha``/``signable`` keys the MCP ``review_code`` tool documents. So
``--source attested`` silently reviewed the dirty working tree and returned an unsignable
result indistinguishable from a successful attested run — on the trust boundary that decides
whether a code review can be signed.

Pinned here: an explicit ``ref`` selects the REVIEWED commit (it lands on the request's
``head``, which is the one ref the gate pins its snapshot at), ``source`` reaches the request,
and the returned result carries the provenance of the handle the review ACTUALLY ran under —
for each ``source`` value.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, ClassVar

import pytest

import rebar
from rebar import schemas
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
    """A rebar repo with an ``origin`` remote so the ATTESTED gate can materialize both the
    pinned code snapshot and the pinned ticket-store clone (mirrors
    test_code_review_gate_session.py)."""
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
    return repo


def _stub_gate_run(monkeypatch) -> None:
    """Drive the four-pass gate to a terminal PASS verdict without any LLM call, leaving the
    REAL snapshot resolution (the thing under test) in place."""
    from rebar.llm.code_review import detectors as _det
    from rebar.llm.workflow import executor as _executor

    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda repo_root=None: True)
    monkeypatch.setattr(_det, "run_security_detectors", lambda **kw: {})

    def _fake_run_workflow(doc, inputs, **kw):
        class _R:
            run_id = "r"
            workflow_name = doc.get("name")
            status = "succeeded"
            terminal_step = None
            terminal_output: ClassVar[dict] = {
                "verdict": "PASS",
                "blocking": [],
                "advisory": [],
                "coverage": {},
            }
            outputs: ClassVar[dict] = {}
            steps: ClassVar[dict] = {}
            error = None

        return _R()

    monkeypatch.setattr(_executor, "run_workflow", _fake_run_workflow)


def test_review_code_forwards_ref_and_source_into_the_gate_request(monkeypatch):
    """An explicit ``ref``/``source`` must REACH the gate request. ``ref`` selects the reviewed
    commit — the gate pins its ONE snapshot ref from ``request.head``, so ``ref`` lands there."""
    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda repo_root=None: True)
    seen: dict[str, Any] = {}

    def _capture(request):
        seen["head"] = request.head
        seen["base"] = request.base
        seen["source"] = request.source
        return {"verdict": "PASS", "blocking": [], "advisory": [], "coverage": {}}

    monkeypatch.setattr(gate_dispatch, "produce_code_review_verdict", _capture)
    from rebar.llm.code_review import review_code

    result = review_code(
        base="HEAD~1",
        head="HEAD",
        diff_text=_DIFF,
        changed_files=["x.py"],
        ref="deadbeefcafe",
        source="local",
    )

    assert seen["source"] == "local", "`source` must reach the request (it selects the read root)"
    assert seen["head"] == "deadbeefcafe", (
        "an explicit `ref` must select the reviewed commit — the gate resolves its snapshot "
        "from request.head, so a dropped `ref` silently reviews the wrong tree"
    )
    assert seen["base"] == "HEAD~1"
    # The reviewed range on the result reflects the ref-selected commit, not the default head.
    assert result["target"]["commits"] == ["HEAD~1", "deadbeefcafe"]


@pytest.mark.parametrize("source", ["local", "attested"])
def test_review_code_result_carries_provenance_for_each_source(
    repo_with_origin, tmp_path, monkeypatch, source
):
    """The returned ``review_result`` must carry ``source``/``verified_at_sha``/``signable`` —
    the promise ``_mcp_llm.review_code`` makes to MCP callers — for BOTH source modes, with a
    pinned SHA + signable ONLY under ``attested``."""
    repo = repo_with_origin
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(tmp_path / "gate-store"))
    monkeypatch.delenv("REBAR_GATE_ALLOW_UNGATED", raising=False)
    _stub_gate_run(monkeypatch)
    from rebar.llm.code_review import review_code

    result = review_code(
        diff_text=_DIFF,
        changed_files=["x.py"],
        source=source,
        repo_root=str(repo),
        runner=FakeRunner(structured={}),
    )

    schemas.validator(schemas.REVIEW_RESULT).validate(result)
    assert result["source"] == source
    if source == "attested":
        sha = result["verified_at_sha"]
        assert isinstance(sha, str) and len(sha) == 40, (
            "attested must record the pinned SHA the review actually read"
        )
        assert sha == _git(repo, "rev-parse", "HEAD")
        assert result["signable"] is True
    else:
        assert result["verified_at_sha"] is None
        assert result["signable"] is False, "a local (dirty-checkout) read is never signable"


def test_config_off_explicit_call_still_dispatches_enabled_and_stays_unpinned(monkeypatch):
    """`verify.enable_code_review` no longer gates the explicit surface (bug
    5b32-37c4-f99a-4315): with the key off, the shim dispatches the gate with
    ``enabled=True``. A stub verdict that pinned nothing must still yield honestly
    UNPINNED provenance — never advertising a signable attested run."""
    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda repo_root=None: False)
    seen: dict[str, Any] = {}

    def _capture(request):
        seen["enabled"] = request.enabled
        return {"verdict": "PASS", "blocking": [], "advisory": [], "coverage": {}}

    monkeypatch.setattr(gate_dispatch, "produce_code_review_verdict", _capture)
    from rebar.llm.code_review import review_code

    result = review_code(diff_text=_DIFF, changed_files=["x.py"])

    assert seen["enabled"] is True  # the explicit call never defers to the config key
    schemas.validator(schemas.REVIEW_RESULT).validate(result)
    assert result["source"] is None
    assert result["verified_at_sha"] is None
    assert result["signable"] is False
