"""`rebar review` is a deprecation shim over `rebar review-plan` (story 316a).

The single-pass ticket-review op is retired: the CLI verb forwards to the plan-review
gate, and the library/MCP surfaces stay wired but signal a registered deprecation. The
behaviours that a *naive* alias would get wrong are what these tests pin:

* the shim must forward ``--no-sign`` so the bare verb keeps writing NO attestation;
* ``--graph`` and a positional ``reviewer_id`` have no counterpart on the target verb and
  must FAIL LOUDLY (argparse exit 2) rather than be accepted and discarded;
* the deprecation signal must fire ONCE per surface — the MCP tool calls the private impl
  so a single tool invocation does not also fire the library ``DeprecationWarning``.
"""

from __future__ import annotations

import subprocess
import warnings
from pathlib import Path

import pytest

import rebar
from rebar._cli import _llm_commands as cli

pytestmark = pytest.mark.unit


@pytest.fixture
def rebar_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    monkeypatch.chdir(repo)
    return repo


def _capture_review_plan(monkeypatch: pytest.MonkeyPatch, calls: list[dict]) -> None:
    """Stub ``rebar.llm.review_plan`` and record every kwarg the shim forwards."""
    import rebar.llm

    def _fake(ticket_id, **kw):  # noqa: ANN001, ANN003
        calls.append({"ticket_id": ticket_id, **kw})
        return {
            "verdict": "PASS",
            "ticket_id": ticket_id,
            "blocking": [],
            "advisory": [],
            "coaching": [],
            "coverage": {"llm_ran": True},
            "runner": "fake",
            "model": "m",
            "source": kw.get("source") or "attested",
            "verified_at_sha": "deadbeef",
            "signable": True,
        }

    monkeypatch.setattr(rebar.llm, "review_plan", _fake)


# ── happy path: the verb forwards, and forwards read-only ─────────────────────
def test_review_forwards_to_review_plan(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1 — `rebar review <id>` runs the plan-review gate and returns its exit code."""
    calls: list[dict] = []
    _capture_review_plan(monkeypatch, calls)
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))

    rc = cli._review([tid])

    assert rc == 0, "a PASS verdict from the plan-review gate must surface as exit 0"
    assert len(calls) == 1, "the shim must invoke the plan-review gate exactly once"
    assert calls[0]["ticket_id"] == tid


def test_shim_forwards_no_sign(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """AC2 — the bare verb writes NO attestation: the shim passes ``sign=False``.

    It also must not silently pass ``force`` — ``--force`` is the audited escape hatch.
    """
    calls: list[dict] = []
    _capture_review_plan(monkeypatch, calls)
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))

    cli._review([tid])

    assert calls[0]["sign"] is False, "the retired verb wrote nothing; the shim must not sign"
    assert not calls[0].get("force"), "the shim must not auto-forward the escape hatch"
    assert "deprecated" in capsys.readouterr().err.lower()


def test_review_forwards_output_and_ref_source(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The flags that exist on BOTH verbs are forwarded verbatim, not dropped."""
    calls: list[dict] = []
    _capture_review_plan(monkeypatch, calls)
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))

    rc = cli._review([tid, "--ref", "release/x", "--source", "local", "-o", "text"])

    assert rc == 0
    assert (calls[0]["ref"], calls[0]["source"]) == ("release/x", "local")
    assert capsys.readouterr().out.strip(), "text output must render, not be swallowed"


def test_review_check_still_reports_backends(
    rebar_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """``--check`` exists on both verbs and keeps working through the shim."""
    import json

    assert cli._review(["--check"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "pydantic_ai" in data


# ── edge: the two arguments the target verb cannot honour must FAIL LOUDLY ────
def test_graph_rejected(rebar_repo: Path, capsys: pytest.CaptureFixture) -> None:
    """AC3a — `--graph` exits 2 with a message naming the replacement verb."""
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))

    with pytest.raises(SystemExit) as exc:
        cli._review([tid, "--graph"])

    assert exc.value.code == 2
    assert "review-plan" in capsys.readouterr().err


def test_positional_reviewer_rejected(rebar_repo: Path, capsys: pytest.CaptureFixture) -> None:
    """AC3b — a positional reviewer_id exits 2 with a message naming the replacement."""
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))

    with pytest.raises(SystemExit) as exc:
        cli._review([tid, "ticket-quality"])

    assert exc.value.code == 2
    assert "review-plan" in capsys.readouterr().err


def test_rejections_happen_before_any_llm_call(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rejections are parse-time: neither spends a gate run."""
    calls: list[dict] = []
    _capture_review_plan(monkeypatch, calls)
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))

    for argv in ([tid, "--graph"], [tid, "ticket-quality"]):
        with pytest.raises(SystemExit):
            cli._review(argv)
    assert calls == []


def test_help_states_the_behaviour_differences(capsys: pytest.CaptureFixture) -> None:
    """AC6 — `--help` names the four differences a caller will notice."""
    with pytest.raises(SystemExit) as exc:
        cli._review(["--help"])
    assert exc.value.code == 0

    text = capsys.readouterr().out.lower()
    for anchor in ("attestation", "blocking", "fast-fail", "multi-pass"):
        assert anchor in text, f"`rebar review --help` must state the {anchor!r} difference"


# ── the accepted, documented divergence: non-claimable fast-fail ──────────────
def test_non_claimable_fast_fails(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """AC7 — a closed ticket exits 2 with NO LLM call, matching `review-plan`."""
    from rebar.llm import runner as runner_mod

    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, repo_root=str(rebar_repo))
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))

    llm_calls: list[object] = []

    def _boom(*a, **kw):  # noqa: ANN002, ANN003
        llm_calls.append(a)
        raise AssertionError("the fast-fail path must not reach the model")

    monkeypatch.setattr(runner_mod, "get_runner", _boom, raising=False)

    # --source local: the fixture repo has no commits, so the default attested snapshot
    # cannot resolve a ref. That is orthogonal to the fast-fail this test pins.
    rc = cli._review([tid, "--source", "local"])

    assert rc == 2, "a non-claimable ticket is INDETERMINATE → exit 2"
    assert llm_calls == []
    combined = capsys.readouterr()
    assert "claimable" in (combined.out + combined.err).lower()


# ── the deprecation registry rows ─────────────────────────────────────────────
@pytest.mark.parametrize(
    "key",
    ["cli:rebar review", "lib:rebar.llm.review_ticket", "mcp:review_ticket"],
)
def test_registry_rows_exist_and_are_scheduled(key: str) -> None:
    """AC4 — three registered rows, each scheduled (not permanent) for v1.0.0."""
    from rebar._deprecations import REGISTRY

    dep = REGISTRY[key]
    assert dep.permanent is False, f"{key} is a supersession, not a rename"
    assert dep.remove_in == "v1.0.0"
    assert "review-plan" in dep.replacement or "review_plan" in dep.replacement


@pytest.mark.parametrize(
    "key",
    ["cli:rebar review", "lib:rebar.llm.review_ticket", "mcp:review_ticket"],
)
def test_warn_deprecated_resolves_each_row(key: str) -> None:
    """``warn_deprecated`` returns a message rather than raising KeyError."""
    from rebar._deprecations import warn_deprecated

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        msg = warn_deprecated(key, via="warning")
    assert "scheduled for removal" in msg


# ── the library split: one signal per surface ─────────────────────────────────
def test_public_warns_impl_is_silent(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC5 — the public op warns; the private impl the internal callers use does not."""
    from rebar.llm import operations
    from rebar.llm.runner import FakeRunner

    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    monkeypatch.setattr(operations, "get_runner", lambda cfg, override=None: FakeRunner())

    with pytest.warns(DeprecationWarning, match="review_ticket"):
        public = rebar.llm.review_ticket(tid, repo_root=str(rebar_repo), source="local")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        private = operations._review_ticket_impl(tid, repo_root=str(rebar_repo), source="local")
    assert [w for w in caught if issubclass(w.category, DeprecationWarning)] == []

    # The split is behaviour-preserving: same shape out of both entry points.
    assert public["findings"] == private["findings"]
    assert public["runner"] == private["runner"]


def test_public_op_is_still_exported() -> None:
    """AC5/AC11 — deprecated is not deleted: the public names still resolve."""
    from rebar.llm import operations

    assert callable(rebar.llm.review_ticket)
    for name in ("assemble_context", "default_reviewer_id", "review_ticket", "select_reviewers"):
        assert name in operations.__all__
    assert operations.default_reviewer_id()  # the ticket-quality default entry survives


def test_internal_callers_use_the_silent_impl() -> None:
    """The three in-source callers must NOT route through the warning wrapper.

    Asserted on the resolved call target, not on source text: each module's own
    reference to the op is the private impl.
    """
    import rebar._mcp_llm as mcp_llm
    from rebar.llm import operations, review_workflows
    from rebar.llm.evals import eval_solver

    for mod in (review_workflows, eval_solver, mcp_llm):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "_review_ticket_impl" in src, f"{mod.__name__} must call the silent impl"
    assert callable(operations._review_ticket_impl)


def test_mcp_tool_emits_one_signal(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC9 — a single MCP `review_ticket` invocation emits exactly ONE signal.

    The tool logs its own ``mcp`` row; because its body calls the private impl it must
    NOT also raise the library ``DeprecationWarning``.
    """
    pytest.importorskip("mcp")
    import asyncio

    from rebar.llm import operations
    from rebar.llm.runner import FakeRunner
    from rebar.mcp_server import build_server

    monkeypatch.setenv("REBAR_MCP_ALLOW_LLM", "1")
    monkeypatch.setattr(operations, "get_runner", lambda cfg, override=None: FakeRunner())
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))

    tools = {t.name: t for t in asyncio.run(build_server().list_tools())}
    assert "review_ticket" in tools, "the MCP tool stays registered (deprecated, not deleted)"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(
            build_server().call_tool("review_ticket", {"ticket_id": tid, "source": "local"})
        )
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert dep == [], f"the MCP path must not double-signal; got {[str(w.message) for w in dep]}"


# ── schema rewiring ──────────────────────────────────────────────────────────
def test_output_schema_for_review_is_the_plan_review_verdict() -> None:
    """AC12 — `rebar review` now emits a plan-review verdict, so the key repoints."""
    from rebar import schemas

    assert schemas.OUTPUT_SCHEMAS["review"] == schemas.PLAN_REVIEW_VERDICT
    assert schemas.OUTPUT_SCHEMAS["review_ticket"] == schemas.REVIEW_RESULT


# ── the LLM-free checks this retirement leaves alone ─────────────────────────
def test_deterministic_checks_are_untouched() -> None:
    """AC10 — clarity-check / check-ac / quality-check keep their own CLI arms."""
    from rebar._cli import _GATES
    from rebar._engine_support import gates as gates_mod

    assert {"clarity-check", "check-ac", "quality-check"} <= set(_GATES)
    for fn in ("clarity_check_cli", "check_ac_cli", "quality_check_cli"):
        assert callable(getattr(gates_mod, fn))
    from rebar._deprecations import REGISTRY

    for key in REGISTRY:
        assert "clarity" not in key and "check-ac" not in key and "quality-check" not in key


# ── docs the retirement must correct ─────────────────────────────────────────
def test_docs_do_not_advertise_the_retired_verb() -> None:
    """AC13 — every surviving `rebar review ` mention marks it deprecated."""
    root = Path(__file__).resolve().parents[2]
    for rel in ("docs/llm-framework.md", "docs/cli-reference.md", "AGENTS.md"):
        for i, line in enumerate((root / rel).read_text(encoding="utf-8").splitlines(), 1):
            if "rebar review " not in line:
                continue
            stripped = line.replace("rebar review-code", "").replace("rebar review-plan", "")
            if "rebar review " not in stripped:
                continue
            assert "deprecat" in line.lower() or "retired" in line.lower(), (
                f"{rel}:{i} still advertises the retired verb: {line.strip()!r}"
            )
