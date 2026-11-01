"""Live regression test for the scope of the cited-reference veto (bug doglike-gossipy-pussycat).

Pass-3 drops a finding when the Pass-2 verifier answers ``cited_reference_accurate == "no"``.
That question tests the FINDING's own evidence. A finding whose claim is that the PLAN cites a
wrong location is correct exactly when the plan's citation is wrong, so it must not be vetoed.
Before the verifier prompts scoped the question, the verifier answered ``no`` for such findings
(case 1 of the bug: a plan citing a nonexistent test module) and the gate dropped them.

The fixture also carries a control finding whose OWN evidence cites a wrong location. The agentic
verifier must still veto that one, so the test fails if a fix simply disables the veto. The
single-turn verifier has no tools and cannot read the control's lines, so for it only the
plan-citation findings are checked.

The verifier reads a small fixture repository built per test, so the cited line numbers never
drift with this checkout. The test needs the external opt-in, the agents package, and a provider
credential. It takes a majority of 3 runs per verifier variant.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import _live_llm
import pytest

pytestmark = pytest.mark.external

# Auto-marks this module's tests `llm_live` (tests/external/conftest.py).
_live_llm_ready = _live_llm.live_llm_ready()

_RUNS = 3
_MAJORITY = 2
_VETO = "veto:cited-reference-inaccurate"

_PROMPT_BY_MODE = {
    "agentic": "plan-review-verifier-agentic",
    "single_turn": "plan-review-verifier",
}

# docs/guide.md: line 3 is a heading; the guide reference is on line 9.
_GUIDE_MD = (
    "# Guide\n\n## Where to read\n\nPointers into the docs follow.\n\n"
    "Plan authors start with the packaged guide.\n\n"
    "Source: `src/pkg/guides.py` names `writing-a-passing-plan.md`.\n"
)
# src/pkg/guides.py: the mapping sits on lines 1-4.
_GUIDES_PY = (
    "AUTHOR_GUIDES = {\n"
    '    "plan": "writing-a-passing-plan.md",\n'
    '    "review": "passing-code-review.md",\n'
    "}\n"
)
_TEST_PY = "def test_ensures_late_binding():\n    assert True\n"

# The plan carries two wrong citations: the test module does not exist, and docs/guide.md:3 is
# a heading rather than the guide reference.
_PLAN = (
    "## Context\n"
    "`init._<name>` reachability is pinned by `tests/test_init_ensures_seam.py:10-14`.\n"
    "The packaged plan guide is already cited at `docs/guide.md:3`.\n"
    "## Approach\n1. Keep the ensure seam; delete the moved doc stub.\n"
    "## Testing\n`pytest tests/test_init_ensures_seam.py`\n"
)

_PLAN_CITATION_FINDINGS = [
    {
        "finding": (
            "The plan cites tests/test_init_ensures_seam.py in Context (':10-14', as the pin for "
            "init._<name> reachability) and as the pytest target in Testing, but no such file "
            "exists in the repository."
        ),
        "criteria": ["G1G2"],
        "evidence": [
            "search_files 'init_ensures_seam' -> no matches anywhere in the repository",
            "tests/ contains only test_ensures_late_binding.py",
        ],
        "impact": "Running the plan's Testing command verbatim errors on a nonexistent path.",
    },
    {
        "finding": (
            "The plan cites docs/guide.md:3 as the line referencing writing-a-passing-plan.md, but "
            "line 3 is the '## Where to read' heading; the reference is at docs/guide.md:9."
        ),
        "criteria": ["E4"],
        "evidence": [
            "docs/guide.md:3 is '## Where to read'",
            "docs/guide.md:9 names writing-a-passing-plan.md",
        ],
        "impact": "A reviewer checking the descoping rationale lands on a heading.",
    },
]

# The control: the finding's OWN evidence cites lines that do not say what it claims.
_CONTROL_FINDING = {
    "finding": (
        "The plan relies on AUTHOR_GUIDES, but src/pkg/guides.py:2 maps 'plan' to 'plan-guide.md', "
        "so the guide basename the plan names is wrong."
    ),
    "criteria": ["E4"],
    "evidence": ['src/pkg/guides.py:2: "plan": "plan-guide.md"'],
    "impact": "The plan deletes the wrong stub.",
}


def _fixture_repo(root: Path) -> Path:
    """A committed git repository holding the files the findings cite."""
    for rel, text in {
        "docs/guide.md": _GUIDE_MD,
        "src/pkg/guides.py": _GUIDES_PY,
        "tests/test_ensures_late_binding.py": _TEST_PY,
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "fixture"], check=True)
    return root


def _verify_once(execution_mode: str, repo: Path) -> dict[int, dict]:
    """One aggregate Pass-2 call over the fixture, inside a local gate session on ``repo``."""
    from rebar.llm import gate_source
    from rebar.llm.config import LLMConfig
    from rebar.llm.prompting import prompts
    from rebar.llm.review_kernel import verify as kverify
    from rebar.llm.runner import RunRequest, get_runner

    cfg = LLMConfig.from_env()
    runner = get_runner(cfg)
    prompt = prompts.get_prompt(_PROMPT_BY_MODE[execution_mode], repo_root=cfg.repo_path)

    def run_chunk(instructions: str, context: str) -> list[dict]:
        system, _meta = prompts.resolve_prompt(
            prompt,
            {"shared_prefix": prompts.shared_plan_prefix(context)},
            repo_root=cfg.repo_path,
        )
        req = RunRequest(
            system_prompt=prompts.strip_volatile_marker(system),
            instructions=instructions,
            config=cfg,
            reviewers=["plan-reviewer"],
            mode="structured",
            output_schema="plan_review_verification",
            execution_mode=execution_mode,
        )
        return runner.run(req).get("verifications", []) or []

    handle = gate_source.resolve_gate_handle(None, gate_source.SOURCE_LOCAL, str(repo), fetch=False)
    with gate_source.gate_read_root(handle):
        result = kverify.verify_findings(
            [*_PLAN_CITATION_FINDINGS, _CONTROL_FINDING],
            context=_PLAN,
            run_chunk=run_chunk,
            window_tokens=1_000_000,
            est_tokens=lambda s: len(s) // 4,
        )
    return result["verifications"]


@_live_llm.skip_without_live_llm
@pytest.mark.parametrize("execution_mode", sorted(_PROMPT_BY_MODE))
def test_live_veto_spares_findings_that_report_a_wrong_plan_citation(
    execution_mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar.llm.review_kernel import decide as kdecide

    repo = _fixture_repo(tmp_path / "fixture")
    monkeypatch.setenv("REBAR_LLM_REPO_PATH", str(repo))
    control_index = len(_PLAN_CITATION_FINDINGS)
    spared_runs = 0  # every plan-citation finding was verified and escaped the veto
    control_runs = 0  # the control's own wrong citation was vetoed
    for _ in range(_RUNS):
        verifications = _verify_once(execution_mode, repo)
        reasons = [
            kdecide.pass3_decide(verifications.get(i))["reason"] for i in range(control_index + 1)
        ]
        if all(r not in (_VETO, "no-verification") for r in reasons[:control_index]):
            spared_runs += 1
        if reasons[control_index] == _VETO:
            control_runs += 1
    assert spared_runs >= _MAJORITY, (
        f"{execution_mode}: only {spared_runs}/{_RUNS} runs kept every finding that reports a "
        "wrong plan citation"
    )
    if execution_mode == "agentic":
        assert control_runs >= _MAJORITY, (
            f"agentic: only {control_runs}/{_RUNS} runs vetoed the control's own wrong citation"
        )
