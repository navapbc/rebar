"""Bounded auto-resume for the completion-verification close gate (ticket b5f8-d3f9-39d9-4195).

When a close FAILs on pure evidence-search exhaustion — every unmet criterion carries the
framework-set ``evidence_sufficient: false`` marker, so nothing was positively refuted — the
correct next action is mechanical: run the verifier again. The cross-run verdict cache
(ticket 8d74) seeds the credited PASSes, so a re-run concentrates its whole budget on the
formerly-insufficient criteria. These tests pin the resumption loop that automates that
re-run at the ONE shared close-gate seam (``close_precheck._completion_precheck`` wrapping
``llm.verify_completion``), bounded by ``verify.auto_resume_max`` AND by strict progress in
the cache-credited PASS count.

Everything is asserted through the real seam: ``verify_completion`` is monkeypatched at
``rebar.llm`` (the module attribute the loop resolves at call time), qualification is the
framework-owned ``completion_reconcile._insufficiency_only`` predicate REUSED (asserted via a
counting wrapper — no parallel reimplementation), and the trail is read off the emitted
sidecar verdict + the refusal message (the close output).
"""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import pytest

import rebar.llm as _llm
from rebar._commands import close_autoresume as _ar
from rebar._commands import close_precheck as _cp
from rebar._commands import gates as _gates
from rebar._commands import transition_close as _tc
from rebar._commands._seam import CommandError
from rebar._engine_support import field_reads as _fr
from rebar.llm import completion_reconcile as _reconcile

pytestmark = pytest.mark.unit


# ── verdict fixtures ─────────────────────────────────────────────────────────────────────────


def _fail_insufficient(credited: int = 0, unmet: int = 2) -> dict:
    """A FAIL whose every unmet record carries the framework insufficiency marker, with
    ``credited`` cache-seeded PASS records (``seeded: true`` + ``met: true``)."""
    criteria = [
        {"criterion": f"C{i}", "met": True, "seeded": True, "evidence": "cached"}
        for i in range(credited)
    ]
    criteria += [
        {"criterion": f"U{i}", "met": False, "evidence_sufficient": False} for i in range(unmet)
    ]
    return {
        "verdict": "FAIL",
        "criteria": criteria,
        "findings": [
            {
                "criterion": f"U{i}",
                "severity": "high",
                "dimension": "completion",
                "detail": "insufficient evidence (search exhausted)",
            }
            for i in range(unmet)
        ],
        "evidence_sufficient": False,
        "certifiable": True,
        "runner": "fake",
    }


def _fail_refuted() -> dict:
    """A FAIL with a GENUINELY refuted criterion (met=false WITHOUT the marker)."""
    return {
        "verdict": "FAIL",
        "criteria": [
            {"criterion": "U0", "met": False, "evidence_sufficient": False},
            {"criterion": "R0", "met": False},  # refutation: no insufficiency marker
        ],
        "findings": [
            {
                "criterion": "R0",
                "severity": "high",
                "dimension": "completion",
                "detail": "positively refuted",
            }
        ],
        "certifiable": True,
        "runner": "fake",
    }


def _pass_verdict(credited: int = 2) -> dict:
    return {
        "verdict": "PASS",
        "criteria": [
            {"criterion": f"C{i}", "met": True, "seeded": True, "evidence": "cached"}
            for i in range(credited)
        ]
        + [{"criterion": "N0", "met": True, "evidence": "fresh"}],
        "findings": [],
        "certifiable": True,
        "runner": "fake",
    }


# ── seam scaffolding (mirrors test_close_gate_bound_message_d59e) ────────────────────────────


def _arm(monkeypatch, results: list[dict], *, max_resumes: int = 2):
    """Enable ONLY the completion close gate, neutralize the DET file-impact precheck and the
    sidecar write (recording the verdicts it would have persisted), bound the loop at
    ``max_resumes``, and script ``verify_completion``'s successive verdicts. Returns
    ``(calls, emitted)`` — the kwargs of each verifier invocation, and each sidecar verdict."""
    monkeypatch.setattr(
        _gates,
        "gate_enabled",
        lambda root, attr, **k: attr == "require_completion_verification_for_close",
    )
    monkeypatch.setattr(_fr, "file_impact", lambda *a, **k: [])
    emitted: list[dict] = []
    monkeypatch.setattr(
        _cp,
        "_emit_completion_sidecar",
        lambda _cs, result, *a, **k: emitted.append(result),
    )
    monkeypatch.setattr(_ar, "_max_resumes", lambda _root: max_resumes)
    calls: list[dict] = []

    def fake_verify(ticket_id, **kwargs):
        calls.append(dict(kwargs, ticket_id=ticket_id))
        return copy.deepcopy(results[min(len(calls) - 1, len(results) - 1)])

    monkeypatch.setattr(_llm, "verify_completion", fake_verify)
    return calls, emitted


def _close(ticket_id: str = "res-0000", ref: str | None = None):
    result, _expectation = _tc._completion_precheck(
        ticket_id, "task", ".", None, reason="", force_close="", ref=ref
    )
    return result


# ── qualification: only an insufficiency-only FAIL resumes ───────────────────────────────────


def test_insufficiency_only_fail_resumes_and_close_proceeds_on_pass(monkeypatch):
    """The 8d74 live case: FAIL on exhaustion, identical re-run PASSes → close proceeds."""
    calls, _ = _arm(monkeypatch, [_fail_insufficient(credited=1), _pass_verdict()])

    result = _close()

    assert len(calls) == 2, "an insufficiency-only FAIL must dispatch exactly one resumption"
    assert result is not None and result["verdict"] == "PASS"


def test_refutation_fails_fast_without_resume(monkeypatch):
    """An unmet record WITHOUT the marker is a genuine refutation — no resume."""
    calls, _ = _arm(monkeypatch, [_fail_refuted()])

    with pytest.raises(CommandError):
        _close()

    assert len(calls) == 1, "a refuted criterion must fail fast — resuming cannot help"


def test_plain_pass_never_resumes(monkeypatch):
    calls, _ = _arm(monkeypatch, [_pass_verdict()])

    result = _close()

    assert len(calls) == 1
    assert result is not None and result["verdict"] == "PASS"


def test_fail_with_zero_unmet_records_does_not_resume(monkeypatch):
    """``_insufficiency_only`` requires at least one unmet record; none → no resume."""
    verdict = {
        "verdict": "FAIL",
        "criteria": [{"criterion": "C0", "met": True}],
        "findings": [
            {
                "criterion": "C0",
                "severity": "high",
                "dimension": "completion",
                "detail": "inconsistent verdict",
            }
        ],
        "certifiable": True,
        "runner": "fake",
    }
    calls, _ = _arm(monkeypatch, [verdict])

    with pytest.raises(CommandError):
        _close()

    assert len(calls) == 1


def test_qualification_reuses_the_framework_insufficiency_predicate(monkeypatch):
    """AC: the loop calls ``completion_reconcile._insufficiency_only`` — the framework-owned
    predicate — rather than reimplementing the marker semantics in parallel."""
    calls, _ = _arm(monkeypatch, [_fail_insufficient(credited=1), _pass_verdict()])
    seen: list[dict] = []
    real = _reconcile._insufficiency_only
    monkeypatch.setattr(
        _reconcile, "_insufficiency_only", lambda result: (seen.append(result), real(result))[1]
    )

    _close()

    assert seen, "the resumption loop must qualify FAILs via _insufficiency_only (reused)"
    assert len(calls) == 2


# ── bounds: verify.auto_resume_max AND strict progress ───────────────────────────────────────


def test_bounded_by_auto_resume_max_and_message_reports_attempts(monkeypatch):
    """Progress every attempt, but the bound caps resumptions: max=2 → exactly 3 verifier
    runs, and the surfaced failure names how many resumptions were attempted."""
    calls, _ = _arm(
        monkeypatch,
        [_fail_insufficient(credited=0), _fail_insufficient(1), _fail_insufficient(2)],
        max_resumes=2,
    )

    with pytest.raises(CommandError) as caught:
        _close()

    assert len(calls) == 3, "max=2 allows the initial attempt plus exactly two resumptions"
    message = str(caught.value)
    assert "auto-resume" in message.lower(), f"the failure must say auto-resume ran: {message}"
    assert "2" in message, f"the failure must state the resumption count: {message}"


def test_auto_resume_max_zero_disables(monkeypatch):
    calls, _ = _arm(monkeypatch, [_fail_insufficient(credited=1)], max_resumes=0)

    with pytest.raises(CommandError):
        _close()

    assert len(calls) == 1, "auto_resume_max=0 must disable resumption entirely"


def test_zero_progress_stops_early_with_resumptions_remaining(monkeypatch):
    """Unchanged cache-credited count → the next re-run would be an identical spin (same
    seeded cache, same budget, same remainder); stop even though max allows more."""
    calls, _ = _arm(
        monkeypatch,
        [_fail_insufficient(credited=1), _fail_insufficient(credited=1)],
        max_resumes=5,
    )

    with pytest.raises(CommandError):
        _close()

    assert len(calls) == 2, "a zero-progress attempt must stop the loop early (no further run)"


def test_progress_then_stall_dispatches_exactly_one_extra_attempt(monkeypatch):
    calls, _ = _arm(
        monkeypatch,
        [
            _fail_insufficient(credited=0),
            _fail_insufficient(credited=2),  # progress → one more resumption dispatches
            _fail_insufficient(credited=2),  # stall → stop
        ],
        max_resumes=5,
    )

    with pytest.raises(CommandError):
        _close()

    assert len(calls) == 3


# ── the resumption re-invokes the SAME verifier entry with the SAME ref ──────────────────────


def test_resumption_keeps_the_pinned_ref_and_attested_source(monkeypatch):
    calls, _ = _arm(monkeypatch, [_fail_insufficient(credited=1), _pass_verdict()])

    _close(ref="deadbeef")

    assert len(calls) == 2
    for kwargs in calls:
        assert kwargs["ref"] == "deadbeef", "every resumption must verify the SAME pinned ref"
        assert kwargs["source"] == "attested"
        assert kwargs["graph"] is False
        assert kwargs["fetch"] is False


# ── trail: per-attempt record on the verdict + in the close output ───────────────────────────


def test_trail_recorded_per_attempt_on_the_emitted_fail_verdict(monkeypatch):
    calls, emitted = _arm(
        monkeypatch,
        [_fail_insufficient(credited=0, unmet=3), _fail_insufficient(credited=1, unmet=2)],
        max_resumes=1,
    )

    with pytest.raises(CommandError) as caught:
        _close()

    assert len(calls) == 2
    assert emitted, "the FAIL verdict must reach the sidecar emit"
    trail = emitted[-1].get("auto_resume_trail")
    assert trail == [
        {"attempt": 1, "cache_credited": 0, "remaining_unmet": 3},
        {"attempt": 2, "cache_credited": 1, "remaining_unmet": 2},
    ]
    message = str(caught.value)
    assert "attempt" in message.lower(), f"the close output must carry the trail: {message}"


def test_trail_present_on_a_pass_reached_via_resume(monkeypatch):
    _arm(monkeypatch, [_fail_insufficient(credited=0), _pass_verdict(credited=2)])

    result = _close()

    assert result is not None
    trail = result.get("auto_resume_trail")
    assert trail is not None and len(trail) == 2
    assert trail[1] == {"attempt": 2, "cache_credited": 2, "remaining_unmet": 0}


def test_single_attempt_close_carries_no_attempts_note(monkeypatch):
    """A no-resume FAIL keeps its exact prior message shape (no misleading resume claim)."""
    calls, _ = _arm(monkeypatch, [_fail_refuted()])

    with pytest.raises(CommandError) as caught:
        _close()

    assert len(calls) == 1
    assert "auto-resume" not in str(caught.value).lower()


def test_sidecar_payload_carries_the_trail_when_present():
    """The durable COMPLETION_VERDICT record keeps the trail on BOTH branches."""
    from rebar.llm import completion_sidecar

    trail = [{"attempt": 1, "cache_credited": 0, "remaining_unmet": 1}]
    fail = dict(_fail_insufficient(unmet=1), ticket_id="t-1", auto_resume_trail=trail)
    passed = dict(_pass_verdict(), ticket_id="t-1", auto_resume_trail=trail)
    plain = dict(_pass_verdict(), ticket_id="t-1")

    assert completion_sidecar.build_payload(fail)["auto_resume_trail"] == trail
    assert completion_sidecar.build_payload(passed)["auto_resume_trail"] == trail
    assert "auto_resume_trail" not in completion_sidecar.build_payload(plain)


# ── config: verify.auto_resume_max (default 2, 0 disables, minimum 0) ────────────────────────


def test_config_default_and_coercion():
    from rebar._config_schema import VerifyConfig
    from rebar._config_sections import _SECTIONS
    from rebar.config import ConfigError

    assert VerifyConfig().auto_resume_max == 2
    coerce = _SECTIONS["verify"]["auto_resume_max"]
    assert coerce(0, "verify.auto_resume_max") == 0
    assert coerce("3", "verify.auto_resume_max") == 3
    with pytest.raises(ConfigError):
        coerce(-1, "verify.auto_resume_max")


# ── the loop sits at the SHARED gate seam: the library path exercises it ─────────────────────


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    import rebar

    rebar.init_repo(repo_root=str(repo))
    return repo


def test_library_path_close_exercises_the_resume_loop(store, monkeypatch):
    """AC: an MCP-or-library-path close flows through the SAME seam — ``rebar.transition``
    -> ``transition_close.close_ticket`` -> ``_completion_precheck`` — so the loop fires
    there too, not only on the CLI path."""
    import rebar

    r = str(store)
    tid = rebar.create_ticket("task", "resume work", description="x" * 60, repo_root=r)
    rebar.transition(tid, "open", "in_progress", repo_root=r)

    monkeypatch.setattr(
        _gates,
        "gate_enabled",
        lambda root, attr, **k: attr == "require_completion_verification_for_close",
    )
    calls: list[str] = []
    verdicts = [_fail_insufficient(credited=1), _pass_verdict()]

    def fake_verify(ticket_id, **kwargs):
        calls.append(ticket_id)
        return copy.deepcopy(verdicts[min(len(calls) - 1, len(verdicts) - 1)])

    monkeypatch.setattr(_llm, "verify_completion", fake_verify)

    rebar.transition(tid, "in_progress", "closed", repo_root=r)

    assert len(calls) == 2, "the library close must resume through the shared seam"
    assert rebar.show_ticket(tid, repo_root=r)["status"] == "closed"
