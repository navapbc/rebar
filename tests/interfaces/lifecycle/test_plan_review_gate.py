"""Claim-gate coverage for the plan-review gate (epic 5fd2).

The gate (rebar._commands.transition._plan_review_precheck, wired into claim_compute) is
opt-in via ``verify.require_plan_review_for_claim``. Unlike the completion CLOSE gate (which
runs the LLM at close time), the CLAIM gate is a FAST, LOCAL signature check — no LLM, no
network — and the heavy three-pass review runs OUT-OF-BAND via ``review_plan`` (driven here with
a FakeRunner, so still no model/network). These tests assert the deterministic behavior:

  * gate OFF (default) → claim without any attestation (today's behavior);
  * gate ON + no attestation → claim BLOCKED (exit 1), ticket stays open;
  * review_plan (clean) signs an attestation → claim then SUCCEEDS;
  * gate ON + --force → claim succeeds, an audit comment records the bypass;
  * a material edit after review INVALIDATES the attestation → claim blocked again;
  * bugs / session_logs are EXEMPT (claim succeeds with no attestation);
  * the DET floor blocks review_plan (no AC) → no signature → claim still blocked;
  * the REVIEW_RESULT sidecar is reducer-IGNORED (status intact; fsck recognises it);
  * the claim path makes NO LLM / NO network call (a pure local HMAC verify).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar import config as _config
from rebar.llm.runner import FakeRunner

_CLEAN = FakeRunner(structured={"analysis": "", "findings": []})

_DESC = (
    "Body with enough length to be a real plan, describing the change in detail so the gate has "
    "something to review and the clarity heuristic is satisfied across the board here.\n\n"
    "## Acceptance Criteria\n- [ ] a thing is observably true\n- [ ] another verifiable check\n\n"
    "## Why\nx\n## What\ny\n## Scope\nz\n"
)


def _enable(repo: Path) -> None:
    (repo / ".rebar").mkdir(exist_ok=True)
    (repo / ".rebar" / "config.conf").write_text("verify.require_plan_review_for_claim = true\n")


def _commit(repo: Path) -> None:
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "c"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _make(repo: Path, ttype: str = "task", desc: str = _DESC) -> str:
    return rebar.create_ticket(ttype, f"plan {ttype}", description=desc, repo_root=str(repo))


def _status(tid: str, repo: Path) -> str:
    return rebar.show_ticket(tid, repo_root=str(repo))["status"]


def _review(tid: str, repo: Path, runner=_CLEAN):
    return rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo))


# ── gate off (default) ─────────────────────────────────────────────────────────
def test_gate_off_by_default_claims_without_attestation(rebar_repo: Path) -> None:
    tid = _make(rebar_repo)
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"


# ── gate on, missing attestation blocks ─────────────────────────────────────────
def test_gate_on_blocks_claim_without_attestation(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.claim(tid, repo_root=str(rebar_repo))
    assert ei.value.returncode == 1
    assert "review-plan" in ei.value.stderr  # the recovery hint names the remedy
    assert _status(tid, rebar_repo) == "open"  # never claimed


# ── earn an attestation → claim succeeds (the full loop) ───────────────────────
def test_review_then_claim_succeeds(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    verdict = _review(tid, rebar_repo)
    assert verdict["verdict"] == "PASS" and verdict["signature"]["signed"]
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"


# ── --force bypass + audit ─────────────────────────────────────────────────────
def test_force_bypasses_gate_with_audit(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    rebar.claim(tid, force="urgent hotfix", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"
    comments = " ".join(
        c.get("body", "")
        for c in rebar.show_ticket(tid, repo_root=str(rebar_repo)).get("comments", [])
    )
    assert "FORCE_CLAIM" in comments and "urgent hotfix" in comments


# ── material-edit invalidation ─────────────────────────────────────────────────
def test_material_edit_invalidates_attestation(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    _review(tid, rebar_repo)
    # Edit the plan's MATERIAL content (description) — no code commit, so HEAD is unchanged.
    rebar.edit_ticket(
        tid,
        description=_DESC + "\nNEW materially-different requirement.",
        repo_root=str(rebar_repo),
    )
    with pytest.raises(rebar.RebarError) as ei:
        rebar.claim(tid, repo_root=str(rebar_repo))
    assert "materially edited" in ei.value.stderr
    assert _status(tid, rebar_repo) == "open"


# ── exemptions ──────────────────────────────────────────────────────────────────
def test_bug_is_exempt_from_claim_gate(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    bug_desc = (
        "A real bug body of sufficient length.\n\n## Reproduction Steps\n1. do x\n\n"
        "Expected: a; Actual: b\n\n## Acceptance Criteria\n- [ ] fixed\n"
    )
    tid = _make(rebar_repo, "bug", desc=bug_desc)
    rebar.claim(tid, repo_root=str(rebar_repo))  # no attestation needed — bugs are exempt
    assert _status(tid, rebar_repo) == "in_progress"


# ── DET-floor block → no signature → claim still blocked ───────────────────────
def test_det_block_yields_no_signature(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    # No `## Acceptance Criteria` ⇒ P1 blocks ⇒ verdict BLOCK ⇒ not signed.
    tid = _make(rebar_repo, desc="A plan body with no acceptance criteria section at all here.")
    verdict = _review(tid, rebar_repo)
    assert verdict["verdict"] == "BLOCK" and not verdict["signature"]["signed"]
    with pytest.raises(rebar.RebarError):
        rebar.claim(tid, repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "open"


# ── REVIEW_RESULT sidecar is reducer-ignored ───────────────────────────────────
def test_sidecar_is_reducer_ignored(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    tid = _make(rebar_repo)  # gate off; just exercise review_plan's sidecar emit
    verdict = _review(tid, rebar_repo)
    assert verdict["sidecar_emitted"] is True
    # The ticket is still readable and its status is unaffected by the sidecar event.
    st = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert st["status"] == "open"
    # A REVIEW_RESULT event file exists on disk (preserved) but is not in compiled state.
    tracker = Path(_config.tracker_dir(str(rebar_repo)))
    from rebar._engine_support.resolver import resolve_ticket_id

    rid = resolve_ticket_id(tid, str(tracker))
    files = list((tracker / rid).glob("*-REVIEW_RESULT.json"))
    assert files, "REVIEW_RESULT event was not written"
    from rebar.reducer._version import is_unknown_newer_type

    assert is_unknown_newer_type("REVIEW_RESULT") is False  # fsck recognises it (no warn)


# ── the claim path makes NO LLM/network call (the 50ms-target structural proof) ─
def test_claim_path_makes_no_llm_call(rebar_repo: Path, monkeypatch) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    _review(tid, rebar_repo)  # earn the signature (this is the out-of-band part)

    # Now poison the LLM op: if the CLAIM path touches it, the test fails.
    def _boom(*a, **k):
        raise AssertionError("claim path must NOT make an LLM call")

    monkeypatch.setattr(rebar.llm, "review_plan", _boom)
    monkeypatch.setattr(rebar.llm, "verify_completion", _boom)
    rebar.claim(tid, repo_root=str(rebar_repo))  # pure local HMAC verify — no LLM
    assert _status(tid, rebar_repo) == "in_progress"


# ── REVIEW_RESULT retention prune bounds growth (db7b AC4) ──────────────────────
def test_sidecar_prune_bounds_growth(rebar_repo: Path) -> None:
    from rebar._engine_support.resolver import resolve_ticket_id
    from rebar.llm.plan_review import sidecar

    _commit(rebar_repo)
    tid = _make(rebar_repo)
    # Emit more sidecars than the retention bound; prune keeps the most-recent `keep`.
    for _ in range(5):
        sidecar.emit(
            {
                "ticket_id": tid,
                "verdict": "PASS",
                "coverage": {},
                "blocking": [],
                "advisory": [],
                "overflow": [],
                "indeterminate": [],
                "dropped": [],
                "coaching": [],
            },
            repo_root=str(rebar_repo),
        )
    tracker = Path(_config.tracker_dir(str(rebar_repo)))
    rid = resolve_ticket_id(tid, str(tracker))
    sidecar.prune(tid, keep=2, repo_root=str(rebar_repo))
    remaining = list((tracker / rid).glob("*-REVIEW_RESULT.json"))
    assert len(remaining) == 2, f"prune should retain 2, found {len(remaining)}"
    # The ticket is still readable (reducer-ignored events never affect state).
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open"


# ── E2E edge case: fail-open on an unavailable LLM (unsupported stack) ──────────
def test_review_fail_open_on_unavailable_llm(rebar_repo: Path) -> None:
    class _BrokenRunner:
        name = "broken"

        def preflight(self):
            raise RuntimeError("the agents extra is missing")

        def run(self, req):  # noqa: ANN001
            raise AssertionError("run must not be reached when preflight fails")

    _commit(rebar_repo)
    tid = _make(rebar_repo)
    v = rebar.llm.review_plan(tid, runner=_BrokenRunner(), repo_root=str(rebar_repo), sign=False)
    # DET floor still ran + passed (AC present) ⇒ no blocks; LLM tier degraded cleanly.
    assert v["verdict"] in ("PASS", "INDETERMINATE")
    assert v["coverage"]["llm_ran"] is False and "llm_error" in v["coverage"]


# ── E2E edge case: cap-hit INDETERMINATE (budget shed) ──────────────────────────
def test_review_cap_hit_indeterminate(rebar_repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("REBAR_PLAN_REVIEW_BUDGET", "0")  # near-zero cap ⇒ shed agent/overlay
    _commit(rebar_repo)
    tid = _make(rebar_repo, "story")
    v = rebar.llm.review_plan(tid, runner=_CLEAN, repo_root=str(rebar_repo), sign=False)
    assert v["coverage"]["budget"]["shed"], "expected agent/overlay criteria shed at cap 0"
    assert any(f.get("reason") == "budget-cap-shed" for f in v["indeterminate"])


# ── config: dotted enables, default off ─────────────────────────────────────────
def test_config_flag_default_off(tmp_path: Path) -> None:
    from rebar import config

    config.reset_config_cache()
    off = tmp_path / "off"
    off.mkdir()
    assert config.load_config(str(off)).verify.require_plan_review_for_claim is False

    config.reset_config_cache()
    on = tmp_path / "on"
    on.mkdir()
    (on / "rebar.toml").write_text("[verify]\nrequire_plan_review_for_claim = true\n")
    assert config.load_config(str(on)).verify.require_plan_review_for_claim is True
