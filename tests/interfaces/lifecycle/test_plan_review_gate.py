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


@pytest.fixture(autouse=True)
def _pin_bespoke_gate_engine(monkeypatch):
    """Pin the BESPOKE gate engine for this module (story B5 cutover).

    These tests drive `review_plan` with a minimal `FakeRunner` and assert the gate's
    DETERMINISTIC, path-independent surface (DET floor, attestation signing, code-drift +
    material-edit invalidation, progressive drift-refresh) — the signing wrapper B5 left
    UNCHANGED, so this behaviour is identical on both engines. The default engine is now
    "workflow", whose verify/coach prompt steps need a real (schema-shaped) runner output a
    fixed-payload `FakeRunner` can't produce. Pinning bespoke keeps these validating the
    still-present bespoke FALLBACK (kept until B-RETIRE); the workflow path is covered by
    tests/unit/workflow/{test_plan_review_workflow,test_plan_review_parity} +
    tests/unit/test_gate_engine_cutover.
    """
    monkeypatch.setenv("REBAR_VERIFY_GATE_ENGINE", "bespoke")


_CLEAN = FakeRunner(structured={"analysis": "", "findings": []})

_DESC = (
    "Body with enough length to be a real plan, describing the change in detail so the gate has "
    "something to review and the clarity heuristic is satisfied across the board here.\n\n"
    "## Acceptance Criteria\n- [ ] a thing is observably true\n- [ ] another verifiable check\n\n"
    "## Why\nx\n## What\ny\n## Scope\nz\n"
)


def _enable(repo: Path, *, progressive: bool = True) -> None:
    (repo / ".rebar").mkdir(exist_ok=True)
    conf = "verify.require_plan_review_for_claim = true\n"
    if progressive:
        conf += "verify.progressive_drift_refresh = true\n"
    (repo / ".rebar" / "config.conf").write_text(conf)


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


# ── LLM unavailable → fail LOUD, never a hollow PASS (fuel-posse-ball) ──────────
def test_review_fails_loud_when_deps_unavailable(rebar_repo: Path) -> None:
    # preflight raises (missing agents extra) ⇒ the LLM tier cannot run ⇒ INDETERMINATE
    # (NOT a DET-only PASS), and never signed.
    from rebar.llm.errors import LLMConfigError

    class _NoDeps:
        name = "no-deps"

        def preflight(self):
            raise LLMConfigError("the 'agents' extra is missing — install nava-rebar[agents]")

        def run(self, req):  # noqa: ANN001
            raise AssertionError("run must not be reached when preflight fails")

    _commit(rebar_repo)
    tid = _make(rebar_repo)
    v = rebar.llm.review_plan(tid, runner=_NoDeps(), repo_root=str(rebar_repo))
    assert v["verdict"] == "INDETERMINATE"  # NOT PASS — no hollow pass
    assert v["coverage"]["llm_ran"] is False and v["coverage"].get("llm_unavailable") is True
    assert not v["signature"]["signed"]  # never signed when the tier did not run


def test_review_fails_loud_when_key_unavailable_at_runtime(rebar_repo: Path) -> None:
    # preflight passes (deps present) but the provider call fails (e.g. missing/invalid
    # API key) — surfaces as LLMUnavailableError from the runner ⇒ INDETERMINATE, unsigned,
    # claim stays blocked. Covers any provider, not just Anthropic.
    from rebar.llm.errors import LLMUnavailableError

    class _NoKey:
        name = "no-key"

        def preflight(self):
            return None  # deps fine

        def run(self, req):  # noqa: ANN001
            raise LLMUnavailableError("the LLM provider call failed: OPENAI_API_KEY not set")

    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    v = rebar.llm.review_plan(tid, runner=_NoKey(), repo_root=str(rebar_repo))
    assert v["verdict"] == "INDETERMINATE" and not v["signature"]["signed"]
    assert v["coverage"].get("llm_unavailable") is True
    with pytest.raises(rebar.RebarError):  # no attestation earned → claim blocked
        rebar.claim(tid, repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "open"


def test_workflow_surfaces_unavailable_llm_as_failed_step(rebar_repo: Path) -> None:
    # The shared contract holds for the OTHER prompt-using client: a workflow whose agent
    # step hits an unavailable LLM reports the run FAILED (not a silently-empty success).
    from rebar.llm.errors import LLMUnavailableError
    from rebar.llm.workflow import executor as _wf

    class _NoKeyAgent(_wf.AgentStepRunner):
        def run(self, ctx):  # noqa: ANN001
            raise LLMUnavailableError("the LLM provider call failed: ANTHROPIC_API_KEY not set")

    doc = {
        "schema_version": "1",
        "name": "wf",
        "steps": [{"id": "s1", "prompt": "code-quality", "mode": "text", "with": {}}],
    }
    res = _wf.run_workflow(doc, agent_runner=_NoKeyAgent(), repo_root=str(rebar_repo))
    assert res.status == "failed" and res.error  # surfaced, not swallowed into success


def test_per_criterion_failure_is_fail_open_when_tier_ran(rebar_repo: Path) -> None:
    # Fail-open PRESERVED at the LLM tier: a NON-systemic per-criterion failure (the tier
    # RAN; a finder raised an ordinary error, not LLMUnavailableError) drops that unit's
    # findings but does NOT mark the tier unavailable → still PASS + signed, claim succeeds.
    # (Distinguishes a systemic outage, which is INDETERMINATE, from a one-off hiccup.)
    class _FlakyFinder:
        name = "flaky"

        def preflight(self):
            return None  # tier IS available

        def run(self, req):  # noqa: ANN001
            raise ValueError("transient parse hiccup for one criterion")  # non-systemic

    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    v = rebar.llm.review_plan(tid, runner=_FlakyFinder(), repo_root=str(rebar_repo))
    assert v["verdict"] == "PASS"  # tier ran (no systemic failure) → NOT INDETERMINATE
    assert v["coverage"]["llm_ran"] is True
    assert v["coverage"].get("llm_unavailable") is not True
    assert v["signature"]["signed"]
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"


# ── E2E edge case: cap-hit INDETERMINATE (budget shed) ──────────────────────────
def test_review_cap_hit_indeterminate(rebar_repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("REBAR_PLAN_REVIEW_BUDGET", "0")  # near-zero cap ⇒ shed agent/overlay
    _commit(rebar_repo)
    tid = _make(rebar_repo, "story")
    v = rebar.llm.review_plan(tid, runner=_CLEAN, repo_root=str(rebar_repo), sign=False)
    assert v["coverage"]["budget"]["shed"], "expected agent/overlay criteria shed at cap 0"
    assert any(f.get("reason") == "budget-cap-shed" for f in v["indeterminate"])


# ── code-drift invalidation (epic boil-golem-veto / ADR 0002) ───────────────────
def _scoped(repo: Path, *, dep: str = "dep.py", content: str = "v = 1\n") -> str:
    """A claimable, reviewed ticket whose attestation is SCOPED to one dependency
    file (via file_impact). Returns the ticket id; the attestation is signed."""
    (repo / dep).write_text(content)
    tid = _make(repo)
    rebar.set_file_impact(
        tid, [{"path": dep, "reason": "the code under review"}], repo_root=str(repo)
    )
    v = _review(tid, repo)
    assert v["signature"]["signed"], "scoped ticket should earn a signed attestation"
    return tid


def test_code_drift_in_dependency_file_invalidates(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _scoped(rebar_repo)
    (rebar_repo / "dep.py").write_text("v = 2  # changed\n")  # drift in the reviewed file
    with pytest.raises(rebar.RebarError) as ei:
        rebar.claim(tid, repo_root=str(rebar_repo))
    assert "drift" in ei.value.stderr.lower()
    assert _status(tid, rebar_repo) == "open"


def test_unrelated_change_does_not_invalidate_attestation(rebar_repo: Path) -> None:
    # The worm-folly-barge scenario: an unrelated commit (HEAD moves) must NOT stale a
    # still-correct, scoped attestation.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _scoped(rebar_repo)
    (rebar_repo / "unrelated.py").write_text("noise = True\n")
    _commit(rebar_repo)  # HEAD advances; dep.py is untouched
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"


def test_dependency_file_deletion_invalidates(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _scoped(rebar_repo)
    (rebar_repo / "dep.py").unlink()  # deleting a reviewed file is drift
    with pytest.raises(rebar.RebarError):
        rebar.claim(tid, repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "open"


def test_empty_dependency_set_falls_back_to_head(rebar_repo: Path) -> None:
    # No file_impact and (FakeRunner) no citations ⇒ unscopable ⇒ conservative
    # whole-HEAD freshness: any commit invalidates.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    assert _review(tid, rebar_repo)["signature"]["signed"]
    _commit(rebar_repo)  # HEAD advances; nothing to scope to
    with pytest.raises(rebar.RebarError) as ei:
        rebar.claim(tid, repo_root=str(rebar_repo))
    assert "stale" in ei.value.stderr.lower()
    assert _status(tid, rebar_repo) == "open"


def test_claim_path_drift_check_is_cheap(rebar_repo: Path) -> None:
    # Times the DRIFT STEP itself (re-hashing the signed dependency paths) over ~30
    # files and asserts it stays in low single-digit ms — the AC's measurable bound.
    # (The no-LLM/no-network property of the claim path is pinned by
    # test_claim_path_makes_no_llm_call above.)
    from rebar import config as _config
    from rebar.llm.plan_review import attest

    _commit(rebar_repo)
    _enable(rebar_repo)
    impact = []
    for i in range(30):
        (rebar_repo / f"d{i}.py").write_text(f"x = {i}\n")
        impact.append({"path": f"d{i}.py", "reason": "r"})
    tid = _make(rebar_repo)
    rebar.set_file_impact(tid, impact, repo_root=str(rebar_repo))
    assert _review(tid, rebar_repo)["signature"]["signed"]

    # Recover the SIGNED {path: hash} map the claim path re-hashes, then time exactly
    # that comparison loop (what claim_gate_check does for drift).
    sig = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    deps = attest.manifest_deps(sig["manifest"])
    assert len(deps) == 30
    base = str(_config.repo_root(str(rebar_repo)))

    def _drift_step() -> list[str]:
        return [p for p, h in deps.items() if attest._hash_file(p, base=base) != h]

    assert _drift_step() == []  # no drift → certified
    best = min(_timed(_drift_step) for _ in range(5))  # min-of-5 ⇒ intrinsic cost, not jitter
    assert best < 0.005, f"drift step too slow ({best * 1000:.2f}ms over 30 files)"


def _timed(fn) -> float:  # noqa: ANN001
    import time

    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


# ── progressive drift-refresh (Story 2, epic boil-golem-veto / ADR 0002) ────────
def test_drift_refresh_reuses_on_immaterial_drift(rebar_repo: Path) -> None:
    # A clean probe (FakeRunner finds nothing) means the drift didn't break the plan →
    # the attestation is REFRESHED (not fully re-reviewed) and the claim then succeeds.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _scoped(rebar_repo)  # signed; dep.py hashed into the manifest
    (rebar_repo / "dep.py").write_text("v = 1  # cosmetic edit; plan still holds\n")  # drift
    v = _review(tid, rebar_repo)
    assert v["coverage"].get("drift_refresh") is True
    assert v["signature"].get("refreshed") is True and v["verdict"] == "PASS"
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"


def test_refreshed_attestation_rebinds_to_current_code(rebar_repo: Path) -> None:
    # The refreshed attestation is re-bound to the CURRENT dependency hashes: a FURTHER
    # drift after the refresh staleness-blocks the claim (no stale reuse).
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _scoped(rebar_repo)
    (rebar_repo / "dep.py").write_text("v = 2\n")
    _review(tid, rebar_repo)  # refresh, now bound to "v = 2"
    (rebar_repo / "dep.py").write_text("v = 3\n")  # drift again
    with pytest.raises(rebar.RebarError) as ei:
        rebar.claim(tid, repo_root=str(rebar_repo))
    assert "drift" in ei.value.stderr.lower()


def test_drift_refresh_skips_on_material_edit(rebar_repo: Path) -> None:
    # A ticket material edit is NOT a drift-only staleness → no refresh; a full review runs.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _scoped(rebar_repo)
    rebar.edit_ticket(
        tid,
        description=_DESC + "\nNEW materially-different requirement.",
        repo_root=str(rebar_repo),
    )
    v = _review(tid, rebar_repo)
    assert "drift_refresh" not in v["coverage"]  # full review, not the progressive path


def test_drift_refresh_skips_on_registry_skew(rebar_repo: Path, monkeypatch) -> None:
    # If the criteria registry changed since signing, the probe's meaning may differ →
    # fall back to a FULL re-review rather than refreshing.
    from rebar.llm.plan_review import attest

    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _scoped(rebar_repo)
    (rebar_repo / "dep.py").write_text("v = 9\n")  # drift
    monkeypatch.setattr(attest, "registry_version", lambda: "different-version-stamp")
    v = _review(tid, rebar_repo)
    assert "drift_refresh" not in v["coverage"]


def test_drift_refresh_escalates_on_probe_finding(rebar_repo: Path, monkeypatch) -> None:
    # A probe finding citing a drifted file means the plan may no longer hold →
    # drift_refresh escalates (returns None) so the caller runs the full review.
    from rebar.llm.config import LLMConfig
    from rebar.llm.plan_review import orchestrator

    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _scoped(rebar_repo)
    (rebar_repo / "dep.py").write_text("v = 42  # material change\n")

    def _block(*a, **k):
        return [
            {
                "decision": "block",
                "criteria": ["E4"],
                "citations": [{"kind": "file", "path": "dep.py"}],
            }
        ]

    monkeypatch.setattr(orchestrator, "_run_passes", _block)
    cfg = LLMConfig.from_env(repo_root=str(rebar_repo))
    ctx = orchestrator.assemble_context(tid, repo_root=str(rebar_repo), cfg=cfg)
    assert orchestrator.drift_refresh(ctx, cfg, runner=_CLEAN, repo_root=str(rebar_repo)) is None


def test_drift_refresh_skips_when_no_prior_verdict(rebar_repo: Path) -> None:
    # First-time review (no prior attestation to reuse) → full review, never the
    # progressive path, even with the flag on.
    _commit(rebar_repo)
    _enable(rebar_repo)  # progressive on
    tid = _make(rebar_repo)
    v = _review(tid, rebar_repo)
    assert "drift_refresh" not in v["coverage"]
    assert v["verdict"] == "PASS" and v["signature"]["signed"]


def test_progressive_drift_refresh_is_opt_in(rebar_repo: Path) -> None:
    # With the flag OFF (default), code drift falls back to a FULL re-review — the
    # progressive path is never taken ("measure before enabling by default").
    _commit(rebar_repo)
    _enable(rebar_repo, progressive=False)
    tid = _scoped(rebar_repo)
    (rebar_repo / "dep.py").write_text("v = 1  # cosmetic\n")  # drift
    v = _review(tid, rebar_repo)
    assert "drift_refresh" not in v["coverage"]  # opt-in: not enabled by default


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
