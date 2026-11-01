"""Live test for the code-review cited-reference veto scope (bug asthmatic-bemused-wyvern).

Pass-3 drops a code-review finding when the Pass-2 verifier answers
``cited_reference_accurate == "no"``. That question tests the FINDING's own evidence. A finding
whose claim is that the CHANGE cites a wrong location is correct exactly when the change's
citation is wrong, so it must not be vetoed. Saved Gerrit reviews showed the verifier answering
``no`` for such findings and the gate dropping them (changes 1547, 1428, 3056).

This test replays one of those real cases: commit ``6c8daf79457`` (the merged form of change
1428) edits ADR 0059's bullet that cites ``capabilities.py:186-206`` for
``_MODEL_ID_CAPABILITY_OVERRIDES``, while the table sits at line 317. The finding is the one the
review-bot saved for that change. A small synthetic fixture did not reproduce the veto on the old
prompt, so the test uses the real commit, checked out from this repository's history; it skips
when the history is not available (a shallow clone).

A control finding whose OWN evidence misquotes the table must still be vetoed, so the test fails
if a fix simply disables the veto. The verifier runs with the gate's settings from
``src/rebar/llm/workflow/gates/code-review.yaml`` (agentic, ``standard`` class, temperature 0).
The test needs the external opt-in, the agents package, and a provider credential. It takes a
majority of 3 runs.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import _live_llm
import pytest

pytestmark = pytest.mark.external

# Auto-marks this module's tests `llm_live` (tests/external/conftest.py).
_live_llm_ready = _live_llm.live_llm_ready()

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMMIT = "6c8daf79457"
_RUNS = 3
_MAJORITY = 2
_VETO = "veto:cited-reference-inaccurate"

# The finding the review-bot saved for change 1428 (dropped by the veto at the time).
_CHANGE_CITATION_FINDING = {
    "finding": (
        "The ADR bullet edited by this diff retains a stale source line reference: it cites "
        "`_MODEL_ID_CAPABILITY_OVERRIDES` at `capabilities.py:186-206`, but the table is "
        "actually defined at capabilities.py:317-355. The diff modified the prose on this exact "
        "line (adding the two Bedrock cells) without correcting the line number, so a reader "
        "following the citation lands in the wrong place."
    ),
    "location": "docs/adr/0059-llm-provider-seam-and-support-tiers.md:132",
    "criteria": ["docs"],
    "severity": "minor",
    "evidence": [
        "src/rebar/llm/capabilities.py:317: `_MODEL_ID_CAPABILITY_OVERRIDES: dict[str, "
        "Mapping[str, object]] = {` — the table's actual location",
        'docs/adr/0059-llm-provider-seam-and-support-tiers.md:132: "`_MODEL_ID_CAPABILITY_'
        'OVERRIDES` (`capabilities.py:186-206`) keys on the EXACT full model id"',
        "The diff added the '...plus the two E1-measured Bedrock cells...' prose but left the "
        "`186-206` anchor untouched",
    ],
}

# The control: its OWN evidence misquotes line 324 (the Sonnet 4.6 row enables native output).
_CONTROL_FINDING = {
    "finding": (
        "The ADR says the Bedrock Sonnet 4.6 cell enables native structured output, but the "
        "table row at src/rebar/llm/capabilities.py:324 disables it, so the ADR misdescribes "
        "the override."
    ),
    "location": "docs/adr/0059-llm-provider-seam-and-support-tiers.md:133",
    "criteria": ["docs"],
    "severity": "minor",
    "evidence": [
        'src/rebar/llm/capabilities.py:324-325: \'"us.anthropic.claude-sonnet-4-6": '
        '{"native_structured_output": False,\''
    ],
}


def _checkout(root: Path) -> tuple[Path, str]:
    """A detached checkout of the replayed commit, and the commit's unified diff."""
    git = ["git", "-C", str(_REPO_ROOT)]
    probe = subprocess.run([*git, "cat-file", "-e", f"{_COMMIT}~1^{{commit}}"], check=False)
    if probe.returncode != 0:
        pytest.skip(f"commit {_COMMIT} and its parent are not in this clone's history")
    subprocess.run(
        [*git, "worktree", "add", "-q", "--detach", str(root), _COMMIT],
        check=True,
        capture_output=True,
    )
    diff = subprocess.run(
        [*git, "diff", f"{_COMMIT}~1", _COMMIT],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return root, diff


def _verify_once(repo: Path, diff_text: str) -> dict[int, dict]:
    """One aggregate code-review Pass-2 call, in a local gate session on ``repo``."""
    from rebar.llm import gate_source
    from rebar.llm.config import LLMConfig
    from rebar.llm.model_classes import resolve_model_string
    from rebar.llm.prompting import prompts
    from rebar.llm.review_kernel import verify as kverify
    from rebar.llm.runner import RunRequest, get_runner

    cfg = LLMConfig.from_env()
    # The gate pins Pass-2 to the `standard` class at temperature 0. Resolve the class from this
    # checkout's config, and the prompt from this checkout's packaged copy (prompt under test).
    cfg = replace(cfg, model=resolve_model_string("standard", str(_REPO_ROOT)), temperature=0.0)
    runner = get_runner(cfg)
    prompt = prompts.get_prompt("code-review-verify", repo_root=str(_REPO_ROOT))

    def run_chunk(instructions: str, context: str) -> list[dict]:
        system, user, _meta = prompts.resolve_prompt_cached(
            prompt,
            {"ticket_context": context, "repo_path": str(repo)},
            base_instructions=instructions,
            repo_root=str(_REPO_ROOT),
        )
        req = RunRequest(
            system_prompt=system,
            instructions=user,
            config=cfg,
            reviewers=["code-reviewer"],
            mode="structured",
            output_schema="code_review_verification",
            execution_mode="agentic",
        )
        return runner.run(req).get("verifications", []) or []

    handle = gate_source.resolve_gate_handle(None, gate_source.SOURCE_LOCAL, str(repo), fetch=False)
    with gate_source.gate_read_root(handle):
        result = kverify.verify_findings(
            [_CHANGE_CITATION_FINDING, _CONTROL_FINDING],
            context=diff_text,
            run_chunk=run_chunk,
            window_tokens=1_000_000,
            est_tokens=lambda s: len(s) // 4,
        )
    return result["verifications"]


@_live_llm.skip_without_live_llm
def test_live_code_review_veto_spares_a_finding_that_reports_a_wrong_change_citation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar.llm.review_kernel import decide as kdecide

    repo, diff_text = _checkout(tmp_path / "replay")
    monkeypatch.setenv("REBAR_LLM_REPO_PATH", str(repo))
    spared_runs = 0  # the change-citation finding was verified and escaped the veto
    control_runs = 0  # the control's own wrong citation was vetoed
    try:
        for _ in range(_RUNS):
            verifications = _verify_once(repo, diff_text)
            reasons = [
                kdecide.pass3_decide(verifications.get(i), impact_fn=kdecide.impact_code)["reason"]
                for i in range(2)
            ]
            if reasons[0] not in (_VETO, "no-verification"):
                spared_runs += 1
            if reasons[1] == _VETO:
                control_runs += 1
    finally:
        subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "worktree", "remove", "--force", str(repo)],
            check=False,
            capture_output=True,
        )
    assert spared_runs >= _MAJORITY, (
        f"only {spared_runs}/{_RUNS} runs kept the finding that reports a wrong citation in the "
        "change"
    )
    assert control_runs >= _MAJORITY, (
        f"only {control_runs}/{_RUNS} runs vetoed the control's own wrong citation"
    )
