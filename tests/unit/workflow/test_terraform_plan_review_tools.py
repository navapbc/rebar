"""REB-640: Terraform structural grounding ↔ plan-review integration.

Covers the seam contract that the happy-path grounding oracle does not:

* the per-CALL tool-provider/finalizer seam on ``RunnerAgentStep`` (Terraform tools reach ONLY a
  Terraform-routed call; a non-Terraform call keeps only its static ``extra_tools``; the
  session finalizer always runs and folds the session's reads into the run's usage sink),
* Pass-1 → Pass-2 routing (a Terraform-scoped Pass-1 finding drives the AGENTIC Pass-2 branch,
  the only branch carrying the re-grounding tools), and
* signed-read-set MEMBERSHIP freshness (a ``.tf`` added under a session's membership glob moves
  the attestation freshness digest, closing the blind spot the concrete per-file reads leave).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from rebar.llm import usage_log
from rebar.llm.plan_review import read_set, terraform_seam, workflow_ops
from rebar.llm.workflow.runs import RunnerAgentStep


def _ctx(inputs: dict) -> SimpleNamespace:
    """A duck-typed StepContext carrying just the ``inputs`` the seam reads."""
    return SimpleNamespace(inputs=inputs)


# ─────────────────────────── routing predicate ───────────────────────────


def test_only_t10_is_terraform_routed() -> None:
    assert terraform_seam.is_terraform_criterion("T10")
    assert not terraform_seam.is_terraform_criterion("E4")
    assert terraform_seam.criteria_are_terraform(["A1", "T10"])
    assert not terraform_seam.criteria_are_terraform(["A1", "E4"])
    assert not terraform_seam.criteria_are_terraform(None)


def test_terraform_evidence_findings_exclude_shed_and_toobig() -> None:
    findings = [
        {"criteria": ["T10"], "location": {"file": "infra/main.tf"}},
        {"criteria": ["E4"]},
        {"criteria": ["T10"], "_shed": True},
        {"criteria": ["T10"], "_too_big": True},
    ]
    routed = terraform_seam.terraform_evidence_findings(findings)
    assert routed == [{"criteria": ["T10"], "location": {"file": "infra/main.tf"}}]
    assert terraform_seam.any_terraform_evidence(findings)
    assert terraform_seam.selected_from_findings(findings) == ["infra/main.tf"]


# ─────────────────── Pass-1 → Pass-2 (agentic) routing ───────────────────


def test_grounding_step_routes_terraform_findings_to_agentic_pass2() -> None:
    # A Terraform-only finding must flip code_grounded True so the AGENTIC verify branch (the
    # one carrying the per-call Terraform tools) runs — a single-turn verify cannot re-ground.
    out = workflow_ops.plan_review_grounding(
        _ctx({"findings": [{"criteria": ["T10"], "location": {"file": "infra/main.tf"}}]})
    )
    assert out == {"code_grounded": True}


def test_grounding_step_ignores_shed_terraform_finding() -> None:
    out = workflow_ops.plan_review_grounding(
        _ctx({"findings": [{"criteria": ["T10"], "_shed": True}]})
    )
    assert out == {"code_grounded": False}


def test_grounding_step_false_for_non_terraform_non_grounded() -> None:
    out = workflow_ops.plan_review_grounding(_ctx({"findings": [{"criteria": ["COH"]}]}))
    assert out == {"code_grounded": False}


# ───────────────────── per-CALL tool-provider seam ───────────────────────


def _run_resolve(step: RunnerAgentStep, ctx: SimpleNamespace):
    """Invoke the private per-call resolver the run loop uses (no live LLM)."""
    return step._resolve_call_tools(ctx)


def test_non_terraform_call_sees_no_terraform_tools(tmp_path: Path) -> None:
    sentinel = object()
    provider = terraform_seam.build_tool_provider(repo_root=str(tmp_path), usage_sink={})
    step = RunnerAgentStep(extra_tools=[sentinel], tool_provider=provider)
    tools, finalize = _run_resolve(step, _ctx({"findings": [{"criteria": ["E4"]}]}))
    # Static extra_tools preserved verbatim; NO Terraform tools appended; no finalizer.
    assert tools == [sentinel]
    assert finalize is None


def test_terraform_call_gets_tools_and_finalizer(tmp_path: Path) -> None:
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra" / "main.tf").write_text(
        'resource "aws_instance" "web" {\n  ami = "ami-1"\n}\n', encoding="utf-8"
    )
    sink: dict = {}
    provider = terraform_seam.build_tool_provider(repo_root=str(tmp_path), usage_sink=sink)
    static = object()
    step = RunnerAgentStep(extra_tools=[static], tool_provider=provider)
    ctx = _ctx({"findings": [{"criteria": ["T10"], "location": {"file": "infra/main.tf"}}]})
    tools, finalize = _run_resolve(step, ctx)
    # Static tool kept, plus the two Terraform refutation query tools.
    assert static in tools
    names = {getattr(t, "__name__", "") for t in tools}
    assert "terraform_lookup_declaration" in names
    assert "terraform_resolve_reference" in names
    assert finalize is not None
    # Issue a real refutation query so the session ledger records reads, then finalize: the
    # finalizer frees the session AND folds its reads into the usage sink deterministically.
    lookup = next(t for t in tools if getattr(t, "__name__", "") == "terraform_lookup_declaration")
    result = lookup("aws_instance.web", module_path="infra")
    assert result["evidence"]["outcome"] == "refuted"
    finalize()
    targets = {f["target"] for f in sink["distinct_fetches"]}
    assert "infra/main.tf" in targets  # concrete read
    assert any(t.endswith("*.tf") for t in targets)  # membership glob


def test_provider_none_when_not_configured() -> None:
    # No tool_provider → the static extra_tools flow through unchanged and no finalizer runs.
    static = object()
    step = RunnerAgentStep(extra_tools=[static])
    tools, finalize = _run_resolve(step, _ctx({"findings": [{"criteria": ["T10"]}]}))
    assert tools == [static]
    assert finalize is None


# ───────────────── signed read-set membership freshness ──────────────────


def test_membership_glob_digest_moves_when_sibling_tf_added(tmp_path: Path) -> None:
    infra = tmp_path / "infra"
    infra.mkdir()
    (infra / "main.tf").write_text('variable "a" {}\n', encoding="utf-8")
    pattern = "infra/**/*.tf"
    before = read_set.glob_membership_digest(pattern, base=str(tmp_path))
    # A NEW sibling .tf under the glob — not read at signing time — must move the membership
    # digest even though it has no baked per-file hash. This is the freshness guard.
    (infra / "network.tf").write_text('variable "b" {}\n', encoding="utf-8")
    after = read_set.glob_membership_digest(pattern, base=str(tmp_path))
    assert before != after


def test_terraform_membership_entries_are_glob_dep_entries() -> None:
    # A session's membership globs validate as glob dependency entries (is_glob True → they get a
    # membership digest via the shared hash_dep_entry boundary), and non-conforming ones drop.
    entries = read_set.terraform_membership_entries(
        ["infra/**/*.tf", "**/*.tf.json", "/abs/*.tf", "notes.py", "../esc/*.tf", "plain/dir"]
    )
    assert entries == ["**/*.tf.json", "infra/**/*.tf"]
    for entry in entries:
        assert read_set.is_glob(entry)


# ─────────────────── synthetic read merge determinism ────────────────────


def test_merge_synthetic_reads_is_order_independent_and_deduped() -> None:
    base = [{"tool": "read_file", "target": "a.tf"}]
    a = usage_log.merge_synthetic_reads(
        base, concrete_reads=["b.tf", "a.tf"], membership_globs=["**/*.tf"]
    )
    b = usage_log.merge_synthetic_reads(
        base, concrete_reads=["a.tf", "b.tf"], membership_globs=["**/*.tf"]
    )
    assert a == b
    targets = [f["target"] for f in a]
    assert targets.count("a.tf") == 1  # dedup against existing fetch
    assert set(targets) == {"a.tf", "b.tf", "**/*.tf"}


# ─────────────── PRODUCTION Pass-1 path: tools ride extra_tools ───────────────
# The live plan-review gate drives the Pass-1 finder through ProductionBatchRunner, which
# DISCARDS the injected agent_runner (its RunnerAgentStep.tool_provider only reaches Pass-2).
# So the T10 finder gets its grounding tools ONLY if they ride RunRequest.extra_tools via
# terraform_seam.pass1_tool_hook threaded run_pass1 → pass1_with_ladder → passes.pass1_chunk —
# the SAME per-criterion seam ``web`` uses (test_web_tool_gating mirrors this for ``web``).


class _CaptureRunner:
    """Runner double recording each RunRequest; returns an empty findings set (no live LLM)."""

    name = "capture"

    def __init__(self) -> None:
        self.reqs: list = []

    def preflight(self) -> None:  # pragma: no cover — protocol completeness
        pass

    def run(self, req):
        self.reqs.append(req)
        return {"findings": [], "_usage": {}}


def _tf_repo(root: Path) -> None:
    (root / "infra").mkdir(parents=True, exist_ok=True)
    (root / "infra" / "main.tf").write_text(
        'variable "x" {\n  default = "y"\n}\n', encoding="utf-8"
    )


def _crits() -> dict:
    from rebar.llm.plan_review import registry

    return {c["id"]: c for c in registry.load_criteria(repo_root=None)}


def _pass1_hook(root: Path, sink: dict):
    return terraform_seam.pass1_tool_hook(
        repo_root=str(root), selected=["infra/main.tf"], usage_sink=sink
    )


def _tool_names(tools) -> set[str]:
    return {str(getattr(t, "__name__", "") or getattr(t, "name", repr(t))).lower() for t in tools}


def _cfg(root: Path):
    from rebar.llm.config import LLMConfig

    return LLMConfig(model="claude-opus-4-8", repo_path=str(root))


def test_t10_agentic_pass1_offers_terraform_tools(tmp_path: Path) -> None:
    from rebar.llm.plan_review import passes

    _tf_repo(tmp_path)
    r = _CaptureRunner()
    hook = _pass1_hook(tmp_path, {})
    passes.pass1_chunk(
        r, _cfg(tmp_path), plan="p", chunk=[_crits()["T10"]], agentic=True, tf_provider=hook
    )
    tools = r.reqs[-1].extra_tools
    assert tools, "T10 agentic Pass-1 must carry Terraform tools in extra_tools"
    assert any("terraform" in n for n in _tool_names(tools))


def test_non_t10_agentic_pass1_offers_no_terraform_tools(tmp_path: Path) -> None:
    from rebar.llm.plan_review import passes

    _tf_repo(tmp_path)
    r = _CaptureRunner()
    hook = _pass1_hook(tmp_path, {})
    passes.pass1_chunk(
        r, _cfg(tmp_path), plan="p", chunk=[_crits()["T1"]], agentic=True, tf_provider=hook
    )
    assert not r.reqs[-1].extra_tools, "a non-Terraform criterion must never see the tools"


def test_t10_single_turn_offers_no_terraform_tools(tmp_path: Path) -> None:
    from rebar.llm.plan_review import passes

    _tf_repo(tmp_path)
    r = _CaptureRunner()
    hook = _pass1_hook(tmp_path, {})
    passes.pass1_chunk(
        r, _cfg(tmp_path), plan="p", chunk=[_crits()["T10"]], agentic=False, tf_provider=hook
    )
    assert not r.reqs[-1].extra_tools, "single-turn calls carry NO tools (defensive parity)"


def test_no_provider_is_byte_neutral(tmp_path: Path) -> None:
    from rebar.llm.plan_review import passes

    _tf_repo(tmp_path)
    r = _CaptureRunner()
    passes.pass1_chunk(r, _cfg(tmp_path), plan="p", chunk=[_crits()["T10"]], agentic=True)
    assert not r.reqs[-1].extra_tools, "no tf_provider → unchanged (no extra_tools)"


def test_finalize_folds_t10_session_reads_into_sink(tmp_path: Path) -> None:
    from rebar.llm.plan_review import passes

    _tf_repo(tmp_path)
    r = _CaptureRunner()
    sink: dict = {}
    hook = _pass1_hook(tmp_path, sink)
    passes.pass1_chunk(
        r, _cfg(tmp_path), plan="p", chunk=[_crits()["T10"]], agentic=True, tf_provider=hook
    )
    assert sink, "the per-call session must be finalized and its reads folded into the sink"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
