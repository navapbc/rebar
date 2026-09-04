"""Code-review Terraform structural grounding oracle (ticket afe3 / REB-640).

These tests specify the code-review counterpart of the already-merged plan-review
Terraform grounding seam.  They assert only observable contracts: routed tool names,
query outcomes/receipts, usage read sets, verdicts, and sidecar dependency hashes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import rebar
from rebar.llm.config import LLMConfig
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow import gate_dispatch

pytestmark = pytest.mark.unit

_TF_TOOLS = {"terraform_lookup_declaration", "terraform_resolve_reference"}


def _ctx(*, prompt: str, inputs: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(step={"prompt": prompt}, inputs=inputs or {})


def _tool_names(tools: Any) -> set[str]:
    return {str(getattr(t, "__name__", "") or getattr(t, "name", "")) for t in (tools or [])}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo_with_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """Small real rebar repo with an origin remote and committed Terraform module."""
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
    _write(
        repo,
        "infra/main.tf",
        'resource "aws_instance" "web" {\n  ami = "ami-1"\n  instance_type = var.size\n}\n',
    )
    _write(repo, "infra/variables.tf", 'variable "size" {\n  default = "t3.micro"\n}\n')
    _write(repo, "app/x.py", "print('hi')\n")
    _git(repo, "add", "infra", "app")
    _git(repo, "commit", "-q", "-m", "content")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main")
    tid = rebar.create_ticket("task", "code-review terraform grounding test", repo_root=str(repo))
    return repo, str(tid)


class _CaptureRunner(FakeRunner):
    """Inner runner double: records prompt -> extra tool names and returns canned payloads."""

    def __init__(self, *, recommend_iac: bool = False, iac_finding: bool = True):
        super().__init__(structured={})
        self.recommend_iac = recommend_iac
        self.iac_finding = iac_finding
        self.calls: list[dict[str, Any]] = []
        self.query_results: list[dict[str, Any]] = []

    def run(self, req):
        prompt = (req.reviewers or [""])[0]
        names = _tool_names(req.extra_tools)
        self.calls.append({"prompt": prompt, "tool_names": names})
        if prompt == "code-review-base":
            return {
                "findings": [],
                "recommend_overlays": (
                    [{"overlay_id": "iac", "reason": "Terraform module changed"}]
                    if self.recommend_iac
                    else []
                ),
            }
        if prompt == "code-review-iac":
            lookup = next(
                (
                    t
                    for t in (req.extra_tools or [])
                    if getattr(t, "__name__", "") == "terraform_lookup_declaration"
                ),
                None,
            )
            if lookup is not None:
                self.query_results.append(
                    {"prompt": prompt, "result": lookup("variable.size", module_path="infra")}
                )
            return {
                "findings": (
                    [
                        {
                            "finding": "Terraform finding cites module",
                            "criteria": ["iac"],
                            "location": "infra/main.tf:1",
                            "evidence": ["infra/main.tf:1"],
                        }
                    ]
                    if self.iac_finding
                    else []
                ),
                "_usage": {"input_tokens": 1, "output_tokens": 1, "requests": 1},
            }
        if prompt == "code-review-verify":
            lookup = next(
                (
                    t
                    for t in (req.extra_tools or [])
                    if getattr(t, "__name__", "") == "terraform_lookup_declaration"
                ),
                None,
            )
            if lookup is not None:
                self.query_results.append(
                    {"prompt": prompt, "result": lookup("variable.size", module_path="infra")}
                )
            return {"verifications": [], "_usage": {"requests": 1}}
        if prompt == "code-review-coach":
            return {"notes": []}
        if req.output_schema == "code_review_findings":
            return {"findings": []}
        return {"findings": [], "recommend_overlays": [], "verifications": [], "notes": []}


def _run_review(
    repo: Path,
    runner: _CaptureRunner,
    *,
    changed_files: list[str],
    diff_text: str,
    monkeypatch: pytest.MonkeyPatch,
    target_ticket: str | None = None,
) -> dict[str, Any]:
    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda repo_root=None: True)
    from rebar.llm.code_review import detectors as _det

    monkeypatch.setattr(_det, "run_security_detectors", lambda **kw: {})
    return gate_dispatch.produce_code_review_verdict(
        gate_dispatch.CodeReviewRequest(
            LLMConfig.from_env(repo_root=str(repo)),
            head="HEAD",
            source="local",
            diff_text=diff_text,
            changed_files=changed_files,
            runner=runner,
            repo_root=str(repo),
            enabled=True,
            target_ticket=target_ticket,
        )
    )


def _calls_for(runner: _CaptureRunner, prompt: str) -> list[dict[str, Any]]:
    return [c for c in runner.calls if c["prompt"] == prompt]


# ─── HAPPY PATH (given to implementer) ───


def test_happy_iac_terraform_findings_select_only_iac_tf_citations() -> None:
    from rebar.llm.code_review.terraform_grounding import (
        any_iac_terraform_evidence,
        iac_terraform_findings,
    )

    findings = [
        {
            "finding": "IaC issue",
            "reviewer_id": "code-review-iac",
            "criteria": ["iac"],
            "location": "infra/main.tf:1",
        },
        {
            "finding": "Security issue",
            "reviewer_id": "code-review-security",
            "criteria": ["security"],
            "location": "infra/main.tf:1",
        },
    ]
    assert iac_terraform_findings(findings) == [findings[0]]
    assert any_iac_terraform_evidence(findings) is True
    assert any_iac_terraform_evidence([findings[1]]) is False


def test_happy_iac_terraform_findings_exclude_shed_and_too_big() -> None:
    from rebar.llm.code_review.terraform_grounding import iac_terraform_findings

    findings = [
        {"reviewer_id": "code-review-iac", "criteria": ["iac"], "location": "infra/main.tf"},
        {
            "reviewer_id": "code-review-iac",
            "criteria": ["iac"],
            "location": "infra/a.tf",
            "_shed": True,
        },
        {
            "reviewer_id": "code-review-iac",
            "criteria": ["iac"],
            "location": "infra/b.tf",
            "_too_big": True,
        },
        {"reviewer_id": "code-review-iac", "criteria": ["iac"], "location": "app.py"},
    ]
    assert iac_terraform_findings(findings) == [findings[0]]


def test_happy_iac_finder_call_gets_tools_finalizer_and_usage(tmp_path: Path) -> None:
    pytest.importorskip("hcl2")
    from rebar.llm.code_review.terraform_grounding import build_code_review_tf_provider

    _write(tmp_path, "infra/main.tf", 'resource "aws_instance" "web" {\n  ami = "ami-1"\n}\n')
    sink: dict[str, Any] = {}
    provider = build_code_review_tf_provider(
        repo_root=str(tmp_path), changed_files=["infra/main.tf"], usage_sink=sink
    )
    provided = provider(_ctx(prompt="code-review-iac"))
    assert provided is not None
    tools, finalize = provided
    assert _TF_TOOLS <= _tool_names(tools)
    lookup = next(t for t in tools if getattr(t, "__name__", "") == "terraform_lookup_declaration")
    result = lookup("aws_instance.web", module_path="infra")
    assert result["evidence"]["outcome"] == "refuted"
    finalize()
    targets = {f["target"] for f in sink["distinct_fetches"]}
    assert "infra/main.tf" in targets
    assert any(t.endswith("*.tf") for t in targets)


# ─── HELD OUT (withheld from implementer) ───


def test_ac1_round_a_iac_overlay_routing_gets_tools_and_base_disjoint(
    repo_with_origin: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _tid = repo_with_origin
    runner = _CaptureRunner()
    _run_review(
        repo,
        runner,
        changed_files=["infra/main.tf"],
        diff_text="diff --git a/infra/main.tf b/infra/main.tf\n+++ b/infra/main.tf\n+resource x\n",
        monkeypatch=monkeypatch,
    )

    iac_calls = _calls_for(runner, "code-review-iac")
    assert len(iac_calls) == 1, "changed Terraform files must dispatch the IaC overlay"
    assert _TF_TOOLS <= iac_calls[0]["tool_names"]
    assert _calls_for(runner, "code-review-base")[0]["tool_names"].isdisjoint(_TF_TOOLS)
    for call in runner.calls:
        if call["prompt"] not in {"code-review-iac", "code-review-verify"}:
            assert call["tool_names"].isdisjoint(_TF_TOOLS), call


def test_ac2_round_b_iac_recommendation_routing_gets_tools_once_whole_module(
    repo_with_origin: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _tid = repo_with_origin
    from rebar.llm.code_review import registry

    monkeypatch.setattr(registry, "glob_triggered_overlays", lambda changed, repo_root=None: [])
    runner = _CaptureRunner(recommend_iac=True)
    _run_review(
        repo,
        runner,
        changed_files=["infra/main.tf"],
        diff_text="diff --git a/infra/main.tf b/infra/main.tf\n+++ b/infra/main.tf\n+resource x\n",
        monkeypatch=monkeypatch,
    )

    iac_calls = _calls_for(runner, "code-review-iac")
    assert len(iac_calls) == 1, "Round-B escalation must not re-run an overlay twice"
    assert _TF_TOOLS <= iac_calls[0]["tool_names"]
    iac_query = next(q for q in runner.query_results if q["prompt"] == "code-review-iac")
    assert iac_query["result"]["evidence"]["outcome"] == "refuted"
    assert iac_query["result"]["evidence"]["location"]["file"] == "infra/variables.tf"


def test_ac3_verifier_routing_gets_distinct_fresh_terraform_session(
    repo_with_origin: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _tid = repo_with_origin
    pytest.importorskip("hcl2")
    from rebar.grounding import terraform_tools as tft

    opened: list[int] = []
    real_open = tft.open_session

    def recording_open_session(*args, **kwargs):
        session = real_open(*args, **kwargs)
        opened.append(id(session))
        return session

    monkeypatch.setattr(tft, "open_session", recording_open_session)
    runner = _CaptureRunner()
    _run_review(
        repo,
        runner,
        changed_files=["infra/main.tf"],
        diff_text="diff --git a/infra/main.tf b/infra/main.tf\n+++ b/infra/main.tf\n+resource x\n",
        monkeypatch=monkeypatch,
    )

    verify_calls = _calls_for(runner, "code-review-verify")
    assert verify_calls and _TF_TOOLS <= verify_calls[0]["tool_names"]
    prompts_with_queries = [q["prompt"] for q in runner.query_results]
    assert prompts_with_queries.count("code-review-iac") == 1
    assert prompts_with_queries.count("code-review-verify") == 1
    assert len(opened) >= 2 and len(set(opened[:2])) == 2
    assert (
        runner.query_results[0]["result"]["receipt"]
        is not runner.query_results[1]["result"]["receipt"]
    )


@pytest.mark.parametrize(
    ("changed_files", "recommend_iac", "iac_finding"),
    [
        (["app/x.py"], True, True),
        (["infra/main.tf"], False, False),
    ],
)
def test_ac4_negative_routing_advertises_no_terraform_tools(
    repo_with_origin: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    changed_files: list[str],
    recommend_iac: bool,
    iac_finding: bool,
) -> None:
    repo, _tid = repo_with_origin
    runner = _CaptureRunner(recommend_iac=recommend_iac, iac_finding=iac_finding)
    diff_path = changed_files[0]
    _run_review(
        repo,
        runner,
        changed_files=changed_files,
        diff_text=f"diff --git a/{diff_path} b/{diff_path}\n+++ b/{diff_path}\n+x\n",
        monkeypatch=monkeypatch,
    )

    for call in runner.calls:
        if changed_files == ["infra/main.tf"] and call["prompt"] == "code-review-iac":
            continue
        assert call["tool_names"].isdisjoint(_TF_TOOLS), call
    if not iac_finding:
        verify = _calls_for(runner, "code-review-verify")
        assert verify and verify[0]["tool_names"].isdisjoint(_TF_TOOLS)


def test_ac4_non_iac_overlay_and_coach_negative_routing_no_tools(
    repo_with_origin: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _tid = repo_with_origin
    from rebar.llm.code_review import registry

    monkeypatch.setattr(
        registry, "glob_triggered_overlays", lambda changed, repo_root=None: ["security"]
    )
    runner = _CaptureRunner(iac_finding=False)
    _run_review(
        repo,
        runner,
        changed_files=["infra/main.tf"],
        diff_text="diff --git a/infra/main.tf b/infra/main.tf\n+++ b/infra/main.tf\n+x\n",
        monkeypatch=monkeypatch,
    )

    assert _calls_for(runner, "code-review-security"), "control must exercise a non-IaC overlay"
    assert _calls_for(runner, "code-review-coach"), "control must exercise the coach call"
    for prompt in ("code-review-base", "code-review-security", "code-review-coach"):
        for call in _calls_for(runner, prompt):
            assert call["tool_names"].isdisjoint(_TF_TOOLS), call


def test_ac5_tfvars_is_direct_evidence_but_not_variable_resolution(tmp_path: Path) -> None:
    """A changed ``.tfvars`` puts its module in query scope (direct evidence), but its assigned
    value is NEVER injected: a ``var.*`` reference resolves to the *declaration site*, not to the
    ``.tfvars`` literal. Routed through afe3's code-review provider (changed set = the .tfvars)."""
    import json

    pytest.importorskip("hcl2")
    from rebar.llm.code_review.terraform_grounding import build_code_review_tf_provider

    _write(
        tmp_path,
        "infra/main.tf",
        'resource "aws_instance" "web" {\n  instance_type = var.size\n}\n',
    )
    _write(tmp_path, "infra/variables.tf", 'variable "size" {\n  default = "t3.micro"\n}\n')
    _write(tmp_path, "infra/prod.tfvars", 'size = "m7i.large"\n')

    sink: dict[str, Any] = {}
    provider = build_code_review_tf_provider(
        repo_root=str(tmp_path), changed_files=["infra/prod.tfvars"], usage_sink=sink
    )
    # A changed .tfvars is in Terraform scope: the provider mints a session (direct evidence).
    provided = provider(_ctx(prompt="code-review-iac"))
    assert provided is not None, "a changed .tfvars must be queryable as direct evidence"
    tools, finalize = provided
    resolve = next(t for t in tools if getattr(t, "__name__", "") == "terraform_resolve_reference")
    try:
        resolved = resolve("var.size", from_file="infra/main.tf")
    finally:
        finalize()

    # The reference resolves to the DECLARATION site, never to the .tfvars assignment; and the
    # concrete .tfvars literal is never injected into the evidence.
    assert resolved["evidence"]["outcome"] == "refuted"
    assert resolved["evidence"]["location"]["file"] == "infra/variables.tf"
    assert "m7i.large" not in json.dumps(resolved)


def test_ac6_sidecar_deps_include_terraform_reads_and_hash_only_changes_when_they_change(
    repo_with_origin: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, target = repo_with_origin
    emitted: list[dict[str, Any]] = []
    from rebar.llm.code_review import sidecar

    def capture_emit(verdict, *, target_ticket, repo_root=None, **kwargs):
        emitted.append(
            {
                "payload": sidecar.build_payload(verdict, target_ticket=target_ticket),
                "verdict": dict(verdict),
            }
        )
        return True

    monkeypatch.setattr(sidecar, "emit", capture_emit)
    runner = _CaptureRunner()
    _run_review(
        repo,
        runner,
        changed_files=["infra/main.tf"],
        diff_text="diff --git a/infra/main.tf b/infra/main.tf\n+++ b/infra/main.tf\n+resource x\n",
        monkeypatch=monkeypatch,
        target_ticket=target,
    )
    deps_before = emitted[-1]["payload"]["deps"]
    fetch_targets = {
        f["target"] for f in emitted[-1]["verdict"].get("_usage", {}).get("distinct_fetches", [])
    }

    assert "infra/main.tf" in deps_before
    assert "infra/variables.tf" in deps_before
    assert {"infra/main.tf", "infra/variables.tf"} <= fetch_targets

    _write(repo, "app/unrelated.txt", "changed\n")
    runner2 = _CaptureRunner()
    _run_review(
        repo,
        runner2,
        changed_files=["infra/main.tf"],
        diff_text="diff --git a/infra/main.tf b/infra/main.tf\n+++ b/infra/main.tf\n+resource x\n",
        monkeypatch=monkeypatch,
        target_ticket=target,
    )
    deps_unrelated = emitted[-1]["payload"]["deps"]
    assert deps_unrelated["infra/variables.tf"] == deps_before["infra/variables.tf"]

    _write(repo, "infra/variables.tf", 'variable "size" {\n  default = "m7i.large"\n}\n')
    runner3 = _CaptureRunner()
    _run_review(
        repo,
        runner3,
        changed_files=["infra/main.tf"],
        diff_text="diff --git a/infra/main.tf b/infra/main.tf\n+++ b/infra/main.tf\n+resource x\n",
        monkeypatch=monkeypatch,
        target_ticket=target,
    )
    deps_after = emitted[-1]["payload"]["deps"]
    assert deps_after["infra/variables.tf"] != deps_before["infra/variables.tf"]


def test_ac7_missing_extra_abstains_fail_open_and_gate_still_returns_ordinary_verdict(
    repo_with_origin: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _tid = repo_with_origin
    from rebar.grounding import terraform_tools as tft
    from rebar.llm.code_review.terraform_grounding import build_code_review_tf_provider

    monkeypatch.setattr(tft, "available", lambda: False)
    sink: dict[str, Any] = {}
    provider = build_code_review_tf_provider(
        repo_root=str(repo), changed_files=["infra/main.tf"], usage_sink=sink
    )
    tools, finalize = provider(_ctx(prompt="code-review-iac"))  # type: ignore[misc]
    lookup = next(t for t in tools if getattr(t, "__name__", "") == "terraform_lookup_declaration")
    result = lookup("aws_instance.web", module_path="infra")
    finalize()

    assert result["evidence"]["outcome"] == "abstain"
    assert result["receipt"]["reason_detail"] == "missing_extra"

    runner = _CaptureRunner(iac_finding=False)
    verdict = _run_review(
        repo,
        runner,
        changed_files=["infra/main.tf"],
        diff_text="diff --git a/infra/main.tf b/infra/main.tf\n+++ b/infra/main.tf\n+resource x\n",
        monkeypatch=monkeypatch,
    )
    assert verdict["verdict"] == "PASS"
    assert verdict["blocking"] == []
    assert verdict["advisory"] == []
