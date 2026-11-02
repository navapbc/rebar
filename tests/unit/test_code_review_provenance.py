"""review_code forwards ref and source into the gate request and returns
the provenance of the handle actually used. An explicit ref selects the
reviewed commit. An unpinned tree cannot be reported as signable.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, ClassVar

import pytest

import rebar
from rebar import schemas
from rebar.llm import gate_source
from rebar.llm.code_review import shim
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
    monkeypatch.setattr(_det, "run_detectors", lambda **kw: {})

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


# ═══════════════════════════════════════════════════════════════════════════
# Provenance may never be FABRICATED: an unpinned review must not claim source,
# verified_at_sha, or signable status it did not earn. These exercise the `shim`
# surface directly, where the gate's own stamp must win over the caller's request.
# ═══════════════════════════════════════════════════════════════════════════

PROVENANCE = ("source", "verified_at_sha", "signable")


def _enable(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any], verdict: dict) -> None:
    """Enable the gate and stub the four-pass run, capturing the request it receives."""
    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda _r: True)

    def _produce(request: Any) -> dict:
        captured["request"] = request
        return verdict

    monkeypatch.setattr(gate_dispatch, "produce_code_review_verdict", _produce)


def _verdict(**over: Any) -> dict:
    base = {"verdict": "PASS", "blocking": [], "advisory": [], "runner": "stub", "coverage": {}}
    base.update(over)
    return base


# ── the caller's request actually reaches the gate ──────────────────────────


def test_source_reaches_the_gate_request(monkeypatch: pytest.MonkeyPatch) -> None:
    cap: dict[str, Any] = {}
    _enable(monkeypatch, cap, _verdict())

    shim.review_code(source="attested", ref="cafebabe1234", diff_text="d")

    assert cap["request"].source == "attested"


def test_explicit_ref_selects_the_reviewed_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate pins exactly one ref (`request.head`). An explicit `--ref` must become
    that commit, or the snapshot is pinned somewhere other than what is being reviewed."""
    cap: dict[str, Any] = {}
    _enable(monkeypatch, cap, _verdict())

    shim.review_code(ref="cafebabe1234", base="HEAD~3", diff_text="d")

    assert cap["request"].head == "cafebabe1234"
    assert cap["request"].base == "HEAD~3", "an explicit ref must not disturb the range base"


def test_absent_ref_leaves_head_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The negative control for the mapping: no `ref` means `head` is untouched, so the
    override cannot be an unconditional clobber."""
    cap: dict[str, Any] = {}
    _enable(monkeypatch, cap, _verdict())

    shim.review_code(head="my-head", diff_text="d")

    assert cap["request"].head == "my-head"


def test_reviewers_input_is_rejected_on_the_library_surface() -> None:
    with pytest.raises(TypeError, match="reviewers"):
        shim.review_code(diff_text="d", reviewers=["code-quality"])


# ── the returned contract is complete on EVERY path ─────────────────────────


def test_result_carries_all_three_provenance_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_mcp_llm` promises these keys unconditionally."""
    cap: dict[str, Any] = {}
    _enable(monkeypatch, cap, _verdict(source="local", verified_at_sha=None, signable=False))

    result = shim.review_code(diff_text="d")

    for key in PROVENANCE:
        assert key in result, f"the documented MCP promise requires {key!r}"


def test_config_off_explicit_call_reaches_the_gate_enabled_and_stays_unpinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The config key no longer gates the explicit surface (bug 5b32-37c4-f99a-4315): with
    `verify.enable_code_review` off, the shim still dispatches `enabled=True`. A verdict
    that pinned nothing must be honestly unpinned — NOT signable: `signable: True` here
    would let an unreviewed change be certified, the worst reachable outcome."""
    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda _r: False)
    cap: dict[str, Any] = {}

    def _produce(request: Any) -> dict:
        cap["request"] = request
        return _verdict()

    monkeypatch.setattr(gate_dispatch, "produce_code_review_verdict", _produce)

    result = shim.review_code(source="attested", ref="cafebabe1234", diff_text="d")

    assert cap["request"].enabled is True  # explicit intent, never the config key
    for key in PROVENANCE:
        assert key in result
    assert result["signable"] is False, "an unpinned result must never be signable"
    assert result["verified_at_sha"] is None


# ── provenance is never FABRICATED ──────────────────────────────────────────


def test_a_verdict_that_pinned_nothing_yields_an_unsignable_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A preflight-degraded verdict never resolved a handle. Claiming a source it did
    not have -- e.g. echoing back the REQUESTED `attested` -- would be exactly the
    silent-unsignable-attested-run confusion this ticket exists to remove."""
    cap: dict[str, Any] = {}
    _enable(monkeypatch, cap, _verdict(verdict="INDETERMINATE"))  # no provenance on it

    result = shim.review_code(source="attested", ref="cafebabe1234", diff_text="d")

    assert result["signable"] is False
    assert result["verified_at_sha"] is None
    assert result["source"] is None, (
        "a run that pinned nothing must not report the REQUESTED source as achieved"
    )


def test_the_gates_own_stamp_wins_over_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """The result must describe what the review ACTUALLY ran under, not what was asked
    for. A caller asking for `attested` that resolves to `local` must see `local`."""
    cap: dict[str, Any] = {}
    _enable(
        monkeypatch,
        cap,
        _verdict(source="local", verified_at_sha=None, signable=False),
    )

    result = shim.review_code(source="attested", diff_text="d")

    assert result["source"] == "local"
    assert result["signable"] is False


def test_attested_stamp_is_propagated_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    cap: dict[str, Any] = {}
    sha = "a" * 40
    _enable(monkeypatch, cap, _verdict(source="attested", verified_at_sha=sha, signable=True))

    result = shim.review_code(source="attested", ref=sha, diff_text="d")

    assert result["source"] == "attested"
    assert result["verified_at_sha"] == sha
    assert result["signable"] is True


# ── the shared helper's own contract ────────────────────────────────────────


def test_copy_provenance_defaults_to_unpinned_for_a_missing_source() -> None:
    dst: dict[str, Any] = {}
    gate_source.copy_provenance(None, dst)
    assert dst["signable"] is False
    assert dst["verified_at_sha"] is None
    assert dst["source"] is None
