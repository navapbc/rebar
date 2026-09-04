"""Held-out oracle (REB-640): the LIVE plan-review gate offers Terraform grounding
tools to the T10 agentic Pass-1 finder via ``RunRequest.extra_tools`` — the seam the
production runner actually consumes.

The production gate runs ``ProductionBatchRunner`` which drives
``run_pass1 -> sizing.pass1_with_ladder -> passes.pass1_chunk`` with the plain
``Runner`` (it discards its ``agent_runner``). ``Runner`` appends ``req.extra_tools``
to the agentic tool list, so a T10 finder call only SEES the Terraform tools if
``pass1_chunk`` populated ``extra_tools`` for that call. This mirrors the existing
per-criterion ``web`` threading proven in ``tests/unit/test_web_tool_gating.py``.

These assertions are on OBSERVABLE request state (what the runner is handed) — never
on internal structure — so a behaviour-preserving refactor keeps them green.
"""

from pathlib import Path

import pytest

pytest.importorskip("hcl2")

from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import passes, registry, terraform_seam
from rebar.llm.runner import RunRequest


class _CaptureRunner:
    """Runner double recording each RunRequest; returns an empty clean chunk."""

    name = "capture"

    def __init__(self) -> None:
        self.reqs: list[RunRequest] = []

    def preflight(self) -> None:  # pragma: no cover — protocol completeness
        pass

    def run(self, req: RunRequest) -> dict:
        self.reqs.append(req)
        return {"findings": [], "_usage": {}}


def _tf_repo(root: Path) -> None:
    (root / "infra").mkdir(parents=True, exist_ok=True)
    (root / "infra" / "main.tf").write_text(
        'variable "x" {\n  default = "y"\n}\n', encoding="utf-8"
    )


def _crits() -> dict[str, dict]:
    return {c["id"]: c for c in registry.load_criteria(repo_root=None)}


def _hook(root: Path, sink: dict):
    # The single mandated production seam: a per-call hook the pass1 finder invokes.
    #   hook(criteria_ids: list[str], agentic: bool) -> (tools, finalize) | None
    # Non-T10 or non-agentic calls MUST get None (no tools, no session minted).
    return terraform_seam.pass1_tool_hook(
        repo_root=str(root), selected=["infra/main.tf"], usage_sink=sink
    )


def _tool_names(tools) -> set[str]:
    out = set()
    for t in tools or []:
        out.add(str(getattr(t, "__name__", getattr(t, "name", repr(t)))).lower())
    return out


def test_t10_agentic_pass1_offers_terraform_tools(tmp_path: Path) -> None:
    _tf_repo(tmp_path)
    cfg = LLMConfig(model="claude-opus-4-8", repo_path=str(tmp_path))
    r = _CaptureRunner()
    passes.pass1_chunk(
        r,
        cfg,
        plan="p",
        chunk=[_crits()["T10"]],
        agentic=True,
        tf_provider=_hook(tmp_path, {}),
    )
    tools = r.reqs[-1].extra_tools
    assert tools, "T10 agentic Pass-1 must carry Terraform tools in extra_tools"
    names = _tool_names(tools)
    assert any(("lookup" in n) or ("resolve" in n) or ("terraform" in n) for n in names), names


def test_non_t10_agentic_pass1_offers_no_terraform_tools(tmp_path: Path) -> None:
    _tf_repo(tmp_path)
    cfg = LLMConfig(model="claude-opus-4-8", repo_path=str(tmp_path))
    r = _CaptureRunner()
    passes.pass1_chunk(
        r,
        cfg,
        plan="p",
        chunk=[_crits()["T1"]],
        agentic=True,
        tf_provider=_hook(tmp_path, {}),
    )
    assert not r.reqs[-1].extra_tools, "a non-Terraform criterion must never see the tools"


def test_t10_single_turn_offers_no_terraform_tools(tmp_path: Path) -> None:
    _tf_repo(tmp_path)
    cfg = LLMConfig(model="claude-opus-4-8", repo_path=str(tmp_path))
    r = _CaptureRunner()
    passes.pass1_chunk(
        r,
        cfg,
        plan="p",
        chunk=[_crits()["T10"]],
        agentic=False,
        tf_provider=_hook(tmp_path, {}),
    )
    assert not r.reqs[-1].extra_tools, "single-turn calls carry NO tools (defensive parity)"


def test_no_provider_is_byte_neutral(tmp_path: Path) -> None:
    _tf_repo(tmp_path)
    cfg = LLMConfig(model="claude-opus-4-8", repo_path=str(tmp_path))
    r = _CaptureRunner()
    passes.pass1_chunk(r, cfg, plan="p", chunk=[_crits()["T10"]], agentic=True)
    assert not r.reqs[-1].extra_tools, "no tf_provider → unchanged (no extra_tools)"


def test_finalize_folds_t10_session_reads_into_sink(tmp_path: Path) -> None:
    _tf_repo(tmp_path)
    cfg = LLMConfig(model="claude-opus-4-8", repo_path=str(tmp_path))
    sink: dict = {}
    r = _CaptureRunner()
    passes.pass1_chunk(
        r,
        cfg,
        plan="p",
        chunk=[_crits()["T10"]],
        agentic=True,
        tf_provider=_hook(tmp_path, sink),
    )
    assert sink, "the per-call session must be finalized and its reads folded into the sink"
