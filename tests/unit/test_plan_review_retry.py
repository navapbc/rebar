"""Exact review-plan retry (story RP-06 S5 — sullen-famished-incatern).

`rebar review-plan <id> --retry` resumes ONLY the exact latest retained review, and only
when that review is a retryable INDETERMINATE with a current, versioned discovery journal:
it reuses the checkpointed findings of the units that already succeeded and issues model
calls ONLY for the missing units, under a FRESH per-invocation attempt budget. An
ineligible latest result (PASS/BLOCK, a non-retryable indeterminate, or a
missing/legacy/corrupt/stale journal) is REFUSED before any model call — an unsigned
INDETERMINATE, zero calls, no sidecar, the full-review remedy — never a silent fallthrough
to a full review.

These are library-level oracles (AC1–AC6). They assert OBSERVABLE behavior only: the exact
called chunk-id sets via a stateful counting fake, the stored discovery journal + retry
lineage, coverage flags, the surfaced-verdict shape, and zero calls on refusal. The real
CLI subprocess end-to-end (AC8) + the flag conflicts (AC5) live in
``test_plan_review_cli_retry.py``; the generated-help drift guard (AC7) in
``test_gen_cli_reference.py``.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar.llm.plan_review import attest, sidecar
from rebar.llm.runner import FakeRunner

# A moderate budget: SOME criteria run (so there is a checkpointed success to reuse) while
# others shed under the cap — the regime the retry's fresh budget + resume seam exist for.
_RETRY_BUDGET = "0.4"
# The single-turn criterion whose chunk the fake fails on the FIRST encounter, producing a
# journaled `failed` unit (distinct from a budget-shed criterion, which emits no unit).
_FAIL_CRITERION = "F1"

_DESC = (
    "A plan body that clears the deterministic readiness floor so the LLM tier runs.\n\n"
    "## What\nchange a thing in `src/thing.py`.\n\n"
    "## Why\nbecause the current behavior is wrong.\n\n"
    "## Acceptance Criteria\n"
    "- [ ] the thing is observably changed\n"
    "- [ ] `pytest tests/unit` proves the change\n"
)


class _FailOnceFake(FakeRunner):
    """Shape-valid OFFLINE runner that COUNTS finder invocations (the "which units ran"
    oracle) and RAISES a non-context error the FIRST time a chunk carrying a target
    criterion is seen — dropping that chunk to a journaled ``failed`` unit — then succeeds
    on every later encounter (so a retry re-runs it cleanly)."""

    name = "fake"

    def __init__(self, fail_ids: set[str]) -> None:
        super().__init__()
        self.finder_calls: list[list[str]] = []
        self.fail_ids = set(fail_ids)
        self._failed_once: set[str] = set()

    def run(self, req) -> dict:
        from rebar.llm import findings as _f

        schema = req.output_schema
        instructions = req.instructions or ""
        if req.mode == "text":
            return {"text": "[fake]", "runner": self.name, "model": None, "trace_id": None}
        if schema == "plan_review_findings":
            m = re.search(r"\(ids: ([^)]*)\)", instructions)
            ids = [s.strip() for s in (m.group(1).split(",") if m else [])]
            for fid in self.fail_ids:
                if fid in ids and fid not in self._failed_once:
                    self._failed_once.add(fid)
                    raise RuntimeError(f"boom {fid}")
            self.finder_calls.append(ids)
            payload: dict = {"analysis": "", "findings": []}
        elif schema == "plan_review_verification":
            payload = {"verifications": []}
        elif schema == "plan_review_coach":
            payload = {"notes": []}
        else:
            payload = {"analysis": "", "findings": []}
        payload = _f.validate_structured(dict(payload), schema)
        return {**payload, "runner": self.name, "model": None, "trace_id": None}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An initialized rebar repo in a temp git dir with a deterministic gate source."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "test@example.com"),
        ("git", "config", "user.name", "Test"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.chdir(repo)
    monkeypatch.delenv("REBAR_USAGE_LOG", raising=False)
    monkeypatch.setenv("REBAR_GATE_SOURCE", "attested")
    monkeypatch.setenv("REBAR_GATE_REF", "HEAD")
    monkeypatch.setenv("REBAR_PLAN_REVIEW_BUDGET", _RETRY_BUDGET)
    rebar.init_repo(repo_root=str(repo))
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _mkticket(repo: Path) -> str:
    """A leaf task with declared file_impact (so the P9 no-file-impact advisory does not
    force PASS) and per-ticket-unique material (so no checkpoint bleeds across tests)."""
    desc = _DESC + f"\nMaterial nonce: {uuid.uuid4().hex}.\n"
    tid = rebar.create_ticket("task", "retry fixture", description=desc, repo_root=str(repo))
    rebar.set_file_impact(tid, [{"path": "src/thing.py", "reason": "c"}], repo_root=str(repo))
    return tid


def _journal_kinds(repo: Path, tid: str) -> list[tuple[str, str]]:
    payload = sidecar.latest_review_result(tid, repo_root=str(repo))
    return [(u["unit_id"], u["kind"]) for u in payload["discovery_journal"]["units"]]


def _sidecar_files(repo: Path, tid: str) -> list[Path]:
    ticket_dir = repo / ".tickets-tracker" / tid
    return sorted(ticket_dir.glob(f"*-{sidecar.EVENT_TYPE}.json"))


def _sidecar_count(repo: Path, tid: str) -> int:
    return len(_sidecar_files(repo, tid))


def _patch_latest_payload(monkeypatch, mutate) -> None:
    """Make ``sidecar.latest_review_result`` return a MUTATED deep-copy of the real latest
    payload (for the synthetic legacy/corrupt/stale edges the fake cannot naturally
    produce), without dirtying the tracked ticket store."""
    import copy

    original = sidecar.latest_review_result

    def _mutated(ticket_id, *, repo_root=None):
        payload = original(ticket_id, repo_root=repo_root)
        if payload is not None:
            payload = copy.deepcopy(payload)
            mutate(payload)
        return payload

    monkeypatch.setattr(sidecar, "latest_review_result", _mutated)


def _indeterminate_with_failed_unit(repo: Path) -> tuple[str, _FailOnceFake]:
    """Produce the AC1 fixture: an INDETERMINATE review whose journal carries a ``failed``
    unit (the F1 chunk the fake raised on) alongside reusable successes."""
    tid = _mkticket(repo)
    runner = _FailOnceFake({_FAIL_CRITERION})
    verdict = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo))
    assert verdict["verdict"] == "INDETERMINATE"
    assert any(kind == "failed" for _, kind in _journal_kinds(repo, tid))
    return tid, runner


# ── AC1: eligible resume calls ONLY the missing unit ────────────────────────────────
def test_retry_resumes_only_the_missing_unit(repo: Path) -> None:
    tid, runner = _indeterminate_with_failed_unit(repo)
    failed_units = [u for u, k in _journal_kinds(repo, tid) if k == "failed"]
    assert len(failed_units) == 1

    before = len(runner.finder_calls)
    verdict = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), retry=True)
    new_calls = runner.finder_calls[before:]

    # Exactly ONE finder call — the previously-failed chunk — and every reused success made
    # zero calls (they resumed from their checkpoint).
    assert len(new_calls) == 1
    failed_ids = failed_units[0].split(":", 1)[1].split(",")
    assert sorted(new_calls[0]) == sorted(failed_ids)
    assert verdict["verdict"] == "INDETERMINATE"
    assert (verdict.get("coverage") or {}).get("retry") is True


# ── AC2: reuse breadth — a second retry re-runs nothing that already succeeded ───────
def test_retry_reuses_recovered_success_on_the_next_retry(repo: Path) -> None:
    tid, runner = _indeterminate_with_failed_unit(repo)
    # First retry recovers the failed unit (it now succeeds and checkpoints).
    rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), retry=True)
    assert not any(k == "failed" for _, k in _journal_kinds(repo, tid))

    before = len(runner.finder_calls)
    rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), retry=True)
    # The recovered unit is now a reusable success, so the next retry re-runs it zero times.
    assert runner.finder_calls[before:] == []


# ── AC2b: eligible resume whose SOLE retryable trigger is a budget-shed criterion ────
def test_retry_resumes_a_budget_shed_only_review(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A review can be a retryable INDETERMINATE with NO failed/cancelled journal unit — its
    # only missing work is AGENT/overlay criteria SHED under the per-plan budget cap (they
    # emit a `budget-cap-shed` finding, never a journal unit). This is the second, distinct
    # retryable-missing trigger, and it must be treated as ELIGIBLE (proceed + resume), never
    # refused as `no-retryable-missing`.
    tid = _mkticket(repo)
    runner = _FailOnceFake(set())  # nothing fails → no `failed`/`cancelled` unit is produced
    verdict = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo))

    # Preconditions: INDETERMINATE from budget-shed ALONE — the journal has only reusable
    # successes, and the sole retryable trigger is the recorded budget-cap-shed findings.
    assert verdict["verdict"] == "INDETERMINATE"
    kinds = {k for _, k in _journal_kinds(repo, tid)}
    assert "failed" not in kinds and "cancelled" not in kinds
    payload = sidecar.latest_review_result(tid, repo_root=str(repo))
    shed_before = {
        c
        for f in (payload.get("findings") or [])
        if f.get("reason") == "budget-cap-shed"
        for c in (f.get("criteria") or [])
    }
    assert shed_before  # the only thing left to do is the shed criteria

    # Resume under a RAISED budget so the previously-shed criteria now fit and actually run —
    # proving the resume RE-RUNS the shed work (teeth), not merely that it was not refused.
    monkeypatch.setenv("REBAR_PLAN_REVIEW_BUDGET", "100")
    before = len(runner.finder_calls)
    v2 = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), retry=True)
    ran_now = {i for call in runner.finder_calls[before:] for i in call}
    cov = v2.get("coverage") or {}

    assert cov.get("retry") is True
    assert cov.get("retry_refused") is not True  # ELIGIBLE — not refused as no-retryable-missing
    assert cov.get("retry_refusal_reason") is None
    assert shed_before <= ran_now  # every previously-shed criterion was resumed and evaluated
    after = sidecar.latest_review_result(tid, repo_root=str(repo))
    still_shed = {
        c
        for f in (after.get("findings") or [])
        if f.get("reason") == "budget-cap-shed"
        for c in (f.get("criteria") or [])
    }
    assert not (shed_before & still_shed)  # the shed backlog is drained by the resume


# ── AC3: each retry gets a FRESH budget; lineage accumulates but never caps ──────────
def test_retry_lineage_accumulates_without_legacy_seeding(repo: Path) -> None:
    tid, runner = _indeterminate_with_failed_unit(repo)
    # A review that was never retried carries NO lineage (no fabricated attempt-0).
    assert sidecar.latest_review_result(tid, repo_root=str(repo)).get("retry_lineage") is None

    rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), retry=True)
    first = sidecar.latest_review_result(tid, repo_root=str(repo))["retry_lineage"]
    assert first["version"] == 1
    assert first["attempts"] == 1
    # Value-level: the recovered `failed` unit issued a real finder call, so this attempt's
    # cumulative usage records at least that one request (not merely the right keys).
    assert first["cumulative_usage"]["requests"] >= 1

    rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), retry=True)
    second = sidecar.latest_review_result(tid, repo_root=str(repo))["retry_lineage"]
    assert second["attempts"] == 2  # cumulative, monotonic — never reset by the fresh budget
    assert set(second["cumulative_usage"]) == {"input_tokens", "output_tokens", "requests"}
    # Value-level accumulation: the second retry re-ran nothing (all reusable), so cumulative
    # usage is monotonic non-decreasing and never shrinks under the fresh per-attempt budget.
    for key in ("input_tokens", "output_tokens", "requests"):
        assert second["cumulative_usage"][key] >= first["cumulative_usage"][key]


# ── AC4: refusals — zero model calls, exit-2 shape, no sidecar, no fallthrough ───────
def test_retry_refuses_when_no_prior_review(repo: Path) -> None:
    tid = _mkticket(repo)
    runner = _FailOnceFake(set())
    verdict = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), retry=True)
    assert verdict["verdict"] == "INDETERMINATE"
    assert (verdict["coverage"] or {}).get("retry_refused") is True
    assert (verdict["coverage"] or {}).get("retry_refusal_reason") == "no-prior-review"
    assert runner.finder_calls == []  # refused BEFORE any model call
    assert verdict.get("sidecar_emitted") is False
    assert _sidecar_count(repo, tid) == 0  # no new sidecar written


def test_retry_refuses_a_pass(repo: Path) -> None:
    tid = _mkticket(repo)
    # A high budget sheds nothing and the fake never fails → a clean PASS.
    import os

    os.environ["REBAR_PLAN_REVIEW_BUDGET"] = "100"
    try:
        clean = _FailOnceFake(set())
        assert rebar.llm.review_plan(tid, runner=clean, repo_root=str(repo))["verdict"] == "PASS"
    finally:
        os.environ["REBAR_PLAN_REVIEW_BUDGET"] = _RETRY_BUDGET
    before = _sidecar_count(repo, tid)

    runner = _FailOnceFake(set())
    verdict = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), retry=True)
    assert (verdict["coverage"] or {}).get("retry_refusal_reason") == "not-indeterminate"
    assert runner.finder_calls == []
    assert _sidecar_count(repo, tid) == before  # refusal wrote nothing


def test_retry_refuses_a_legacy_payload_without_a_journal(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid, _ = _indeterminate_with_failed_unit(repo)
    _patch_latest_payload(monkeypatch, lambda p: p.pop("discovery_journal", None))
    before = _sidecar_count(repo, tid)

    runner = _FailOnceFake(set())
    verdict = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), retry=True)
    assert (verdict["coverage"] or {}).get("retry_refusal_reason") == "no-journal"
    assert runner.finder_calls == []
    assert _sidecar_count(repo, tid) == before


def test_retry_refuses_a_corrupt_journal_version(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid, _ = _indeterminate_with_failed_unit(repo)
    _patch_latest_payload(monkeypatch, lambda p: p["discovery_journal"].__setitem__("version", 999))
    runner = _FailOnceFake(set())
    verdict = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), retry=True)
    assert (verdict["coverage"] or {}).get("retry_refusal_reason") == "no-journal"
    assert runner.finder_calls == []


def test_retry_refuses_when_no_retryable_missing_unit(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid, _ = _indeterminate_with_failed_unit(repo)

    def _all_success(payload: dict) -> None:
        for unit in payload["discovery_journal"]["units"]:
            unit["kind"] = "success"
        payload["findings"] = [
            f for f in payload.get("findings") or [] if f.get("reason") != "budget-cap-shed"
        ]

    _patch_latest_payload(monkeypatch, _all_success)
    runner = _FailOnceFake(set())
    verdict = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), retry=True)
    assert (verdict["coverage"] or {}).get("retry_refusal_reason") == "no-retryable-missing"
    assert runner.finder_calls == []


def test_retry_refuses_a_stale_review(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tid, _ = _indeterminate_with_failed_unit(repo)
    # The plan's material moved since the review → the stored digests no longer match.
    _patch_latest_payload(
        monkeypatch, lambda p: p.__setitem__("material_fingerprint", "stale-does-not-match")
    )
    runner = _FailOnceFake(set())
    verdict = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), retry=True)
    assert (verdict["coverage"] or {}).get("retry_refusal_reason") == "stale"
    assert runner.finder_calls == []


def test_retry_fails_closed_when_current_material_is_uncomputable(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # current_material_fingerprint returns None on a read error (documented fail-closed),
    # and a payload stamp can also be None — a bare `!=` would read None==None as a MATCH
    # and resume against unconfirmable material. The gate must treat unknown as STALE.
    tid, _ = _indeterminate_with_failed_unit(repo)
    _patch_latest_payload(monkeypatch, lambda p: p.__setitem__("material_fingerprint", None))
    monkeypatch.setattr(attest, "current_material_fingerprint", lambda *a, **k: None)
    runner = _FailOnceFake(set())
    verdict = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), retry=True)
    assert (verdict["coverage"] or {}).get("retry_refusal_reason") == "stale"
    assert runner.finder_calls == []  # fail-closed: no model call against unknown material


def test_retry_fails_closed_when_current_code_sha_is_uncomputable(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same fail-closed posture for the review-code SHA: an uncomputable current SHA (None)
    # must refuse even when the payload's own verified_at_sha is also None.
    tid, _ = _indeterminate_with_failed_unit(repo)
    _patch_latest_payload(monkeypatch, lambda p: p.__setitem__("verified_at_sha", None))
    monkeypatch.setattr(sidecar, "review_code_sha", lambda *a, **k: None)
    runner = _FailOnceFake(set())
    verdict = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), retry=True)
    assert (verdict["coverage"] or {}).get("retry_refusal_reason") == "stale"
    assert runner.finder_calls == []


def test_retry_refuses_on_verified_at_sha_mismatch(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An independent stale sub-branch: the reviewed code SHA moved since the review.
    tid, _ = _indeterminate_with_failed_unit(repo)
    _patch_latest_payload(monkeypatch, lambda p: p.__setitem__("verified_at_sha", "0" * 40))
    runner = _FailOnceFake(set())
    verdict = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), retry=True)
    assert (verdict["coverage"] or {}).get("retry_refusal_reason") == "stale"
    assert runner.finder_calls == []


def test_retry_refuses_on_registry_version_mismatch(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An independent stale sub-branch: the criteria-registry version moved.
    tid, _ = _indeterminate_with_failed_unit(repo)
    _patch_latest_payload(monkeypatch, lambda p: p.__setitem__("regver", "regver-does-not-match"))
    runner = _FailOnceFake(set())
    verdict = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), retry=True)
    assert (verdict["coverage"] or {}).get("retry_refusal_reason") == "stale"
    assert runner.finder_calls == []


def test_retry_refuses_when_a_reusable_checkpoint_no_longer_loads(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An independent stale sub-branch: a reusable success's checkpoint digest no longer
    # resolves (missing/corrupt/legacy-namespace), so its stored success cannot be reused.
    tid, _ = _indeterminate_with_failed_unit(repo)

    def _break_a_reusable_digest(payload: dict) -> None:
        for unit in payload["discovery_journal"]["units"]:
            if unit["kind"] in ("success", "resumed"):
                unit["lineage"]["digest"] = "deadbeef" * 8
                return

    _patch_latest_payload(monkeypatch, _break_a_reusable_digest)
    runner = _FailOnceFake(set())
    verdict = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), retry=True)
    assert (verdict["coverage"] or {}).get("retry_refusal_reason") == "stale"
    assert runner.finder_calls == []


# ── AC5: a normal review (no --retry) is unaffected ─────────────────────────────────
def test_no_flag_review_carries_no_retry_markers(repo: Path) -> None:
    tid = _mkticket(repo)
    runner = _FailOnceFake(set())
    verdict = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo))
    coverage = verdict.get("coverage") or {}
    assert "retry" not in coverage
    assert "retry_refused" not in coverage
    assert sidecar.latest_review_result(tid, repo_root=str(repo)).get("retry_lineage") is None


# ── AC6: the surfaced verdict stays narrow — the journal is never in public output ──
def test_surfaced_verdict_never_exposes_the_discovery_journal(repo: Path) -> None:
    tid, runner = _indeterminate_with_failed_unit(repo)
    # The journal IS persisted to the sidecar (seeds eligibility)…
    assert sidecar.latest_review_result(tid, repo_root=str(repo)).get("discovery_journal")

    normal = rebar.llm.review_plan(tid, runner=_FailOnceFake(set()), repo_root=str(repo))
    retried = rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), retry=True)
    for verdict in (normal, retried):
        coverage = verdict.get("coverage") or {}
        # …but NEVER surfaced: no per-unit trace / verbose journal in the returned verdict.
        assert "discovery_trace" not in coverage
        assert "discovery_journal" not in coverage
        assert "checkpoint" not in coverage
