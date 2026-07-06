"""Story 1669: the code-review false-positive ledger + advisory telemetry.

Two units, both offline (no live LLM):

* ``compile_fp_ledger`` — drafts no-fire eval cases from ``fp:code-review`` tickets (correct
  shape, idempotent via the ``compiled`` tag, malformed-ticket skipped, store-error → []). Uses
  a REAL temporary rebar store (the same idiom as ``test_code_review_scope_intent.py``).
* ``_attach_code_review_metrics`` — the advisory enrichment on the code-review verdict, driven
  by a fake ``MemoryRecorder``; the load-bearing assertion is that the enrichment NEVER changes
  ``verdict['verdict']``.
"""

from __future__ import annotations

import subprocess

import pytest

import rebar
from rebar.llm.code_review import fp_ledger

pytestmark = pytest.mark.unit


# ── compile_fp_ledger (real ticket store) ────────────────────────────────────────────
_LEDGER_BODY = """## Finding
criterion: code-review-tests
finding: flagged a valid observable-postcondition test as tautological.

## Root cause
root-cause: false-evidence

## Diff / context
```diff
--- a/tests/test_claim.py
+++ b/tests/test_claim.py
@@ -1,3 +1,5 @@
+def test_claim_conflict_raises(store):
+    with pytest.raises(ConcurrencyError):
+        store.claim("t1", "b")
```

## Acceptance Criteria
- [ ] the tests overlay no longer fires on this diff
"""


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A real ticket store rooted under tmp_path (REBAR_ROOT), mirroring the scope-intent test."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "t@e.com"), ("config", "user.name", "T")):
        subprocess.run(["git", *args], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return str(repo)


def _make_ledger_ticket(repo, *, body=_LEDGER_BODY, tags=("fp:code-review",)):
    res = rebar.create_ticket(
        "bug",
        "FP: tests overlay over-fired",
        description=body,
        tags=list(tags),
        return_alias=True,
        repo_root=repo,
    )
    return res["id"] if isinstance(res, dict) else res


def test_compile_drafts_correctly_shaped_no_fire_case(store):
    _make_ledger_ticket(store)
    cases = fp_ledger.compile_fp_ledger(repo_root=store)
    assert len(cases) == 1
    case = cases[0]
    assert case["corpus"] == "fp-ledger"
    assert case["expect"] == "pass"
    assert case["mode"] == "false-evidence"
    assert case["id"].startswith("FP-")
    assert "test_claim_conflict_raises" in case["diff"]
    # the fenced info-string / fences themselves are not part of the diff body
    assert "```" not in case["diff"]


def test_compile_is_idempotent_via_compiled_tag(store):
    _make_ledger_ticket(store)
    first = fp_ledger.compile_fp_ledger(repo_root=store)
    assert len(first) == 1
    # the ticket is now tagged `compiled` → a re-run drafts nothing
    second = fp_ledger.compile_fp_ledger(repo_root=store)
    assert second == []


def test_compile_skips_already_compiled_ticket(store):
    _make_ledger_ticket(store, tags=("fp:code-review", "compiled"))
    assert fp_ledger.compile_fp_ledger(repo_root=store) == []


def test_compile_skips_malformed_ticket_without_raising(store):
    # no root-cause line and no fenced diff → skipped, not fatal
    _make_ledger_ticket(store, body="## Finding\njust prose, no root-cause and no diff block.\n")
    assert fp_ledger.compile_fp_ledger(repo_root=store) == []


def test_compile_skips_unknown_root_cause(store):
    body = _LEDGER_BODY.replace("root-cause: false-evidence", "root-cause: not-a-real-enum")
    _make_ledger_ticket(store, body=body)
    assert fp_ledger.compile_fp_ledger(repo_root=store) == []


def test_compile_returns_empty_on_store_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(rebar, "list_tickets", _boom)
    # a store-read error surfaces as [], never raises into the caller
    assert fp_ledger.compile_fp_ledger(repo_root="/does/not/matter") == []


def test_is_non_trivial_diff_thresholds():
    assert fp_ledger.is_non_trivial_diff(1, 21) is True  # > NON_TRIVIAL_DIFF_LINES
    assert fp_ledger.is_non_trivial_diff(2, 3) is True  # > 1 file
    assert fp_ledger.is_non_trivial_diff(1, 20) is False  # exactly at the line threshold, 1 file


def test_root_causes_are_the_closed_five():
    assert fp_ledger.FP_ROOT_CAUSES == frozenset(
        {
            "false-evidence",
            "rubric-overapplication",
            "hallucinated-gap",
            "scope-mismatch",
            "stale-baseline",
        }
    )


# ── _attach_code_review_metrics (advisory enrichment) ─────────────────────────────────
from rebar.llm.workflow import gate_dispatch  # noqa: E402
from rebar.llm.workflow.recorder import MemoryRecorder  # noqa: E402


def _diff_context(n_added_lines: int, n_files: int = 1) -> str:
    """A composed `assemble_diff` context string with `n_added_lines` body `+` lines."""
    body = "\n".join(f"+line {i}" for i in range(n_added_lines))
    return f"## Changed files ({n_files})\n## Diff\n```diff\n--- a/x\n+++ b/x\n{body}\n```"


def _rec(*, changed_files, context, verify_requests, dropped, blocking=(), surfaced=()):
    rec = MemoryRecorder()
    rec.steps = [
        {
            "step_id": "assemble_diff",
            "kind": "uses",
            "status": "succeeded",
            "outputs": {"changed_files": list(changed_files), "context": context},
        },
        {
            "step_id": "verify",
            "kind": "agent",
            "status": "succeeded",
            "duration_ms": 10.0,
            "outputs": {"_usage": {"requests": verify_requests}},
        },
        {
            "step_id": "decide",
            "kind": "uses",
            "status": "succeeded",
            "outputs": {
                "blocking": list(blocking),
                "surfaced": list(surfaced),
                "dropped": list(dropped),
            },
        },
    ]
    return rec


def test_metrics_populated_findings_verify_grounding():
    verdict = {
        "verdict": "PASS",
        "blocking": [],
        "advisory": [{"id": "a", "priority": 0.3}],
        "coverage": {},
    }
    rec = _rec(
        changed_files=["a.py"],
        context=_diff_context(5),  # trivial diff
        verify_requests=4,
        dropped=[],
        surfaced=[{"id": "a", "priority": 0.3}],
    )
    gate_dispatch._attach_code_review_metrics(verdict, rec, total_ms=123.4)
    m = verdict["coverage"]["metrics"]
    assert m["findings_per_run"] == 1  # 0 blocking + 1 advisory
    assert m["verify_requests"] == 4
    assert m["grounding_health"] == "ok"  # trivial diff → never low
    assert "grounding_note" not in verdict["coverage"]


def test_grounding_note_fires_only_on_nontrivial_diff_and_zero_verify():
    verdict = {"verdict": "PASS", "blocking": [], "advisory": [], "coverage": {}}
    rec = _rec(
        changed_files=["a.py", "b.py"],  # >1 file → non-trivial
        context=_diff_context(30),
        verify_requests=0,  # verifier made no model requests
        dropped=[],
    )
    gate_dispatch._attach_code_review_metrics(verdict, rec, total_ms=1.0)
    assert verdict["coverage"]["metrics"]["grounding_health"] == "low"
    assert "grounding_note" in verdict["coverage"]
    assert "20" in verdict["coverage"]["grounding_note"]  # names the threshold


def test_grounding_note_absent_when_verifier_ran():
    verdict = {"verdict": "PASS", "blocking": [], "advisory": [], "coverage": {}}
    rec = _rec(
        changed_files=["a.py", "b.py"],
        context=_diff_context(30),
        verify_requests=7,  # verifier DID run
        dropped=[],
    )
    gate_dispatch._attach_code_review_metrics(verdict, rec, total_ms=1.0)
    assert verdict["coverage"]["metrics"]["grounding_health"] == "ok"
    assert "grounding_note" not in verdict["coverage"]


def test_approach_viability_note_fires_on_high_priority_survivors():
    advisory = [{"id": str(i), "priority": 0.8} for i in range(3)]  # 3 high-priority survivors
    verdict = {"verdict": "PASS", "blocking": [], "advisory": advisory, "coverage": {}}
    rec = _rec(
        changed_files=["a.py"],
        context=_diff_context(5),
        verify_requests=3,
        dropped=[],
        surfaced=advisory,
    )
    gate_dispatch._attach_code_review_metrics(verdict, rec, total_ms=1.0)
    assert "approach_viability_note" in verdict["coverage"]


def test_approach_viability_note_fires_on_high_drop_rate():
    verdict = {
        "verdict": "PASS",
        "blocking": [],
        "advisory": [{"id": "a", "priority": 0.1}],
        "coverage": {},
    }
    # dropped=2, surfaced=1, blocking=0 → drop-rate 2/3 ≥ 0.5
    rec = _rec(
        changed_files=["a.py"],
        context=_diff_context(5),
        verify_requests=3,
        dropped=[{"id": "d1"}, {"id": "d2"}],
        surfaced=[{"id": "a", "priority": 0.1}],
    )
    gate_dispatch._attach_code_review_metrics(verdict, rec, total_ms=1.0)
    assert "approach_viability_note" in verdict["coverage"]


def test_approach_viability_note_absent_below_thresholds():
    verdict = {
        "verdict": "PASS",
        "blocking": [],
        "advisory": [{"id": "a", "priority": 0.1}],
        "coverage": {},
    }
    rec = _rec(
        changed_files=["a.py"],
        context=_diff_context(5),
        verify_requests=3,
        dropped=[],
        surfaced=[{"id": "a", "priority": 0.1}],
    )
    gate_dispatch._attach_code_review_metrics(verdict, rec, total_ms=1.0)
    assert "approach_viability_note" not in verdict["coverage"]


def test_enrichment_never_changes_the_verdict():
    """CRITICAL: the advisory enrichment is observational — verdict['verdict'] is UNCHANGED,
    and no blocking source is added, even under the low-grounding + viability trigger conditions."""
    for original in ("PASS", "BLOCK", "INDETERMINATE"):
        advisory = [{"id": str(i), "priority": 0.9} for i in range(4)]
        verdict = {
            "verdict": original,
            "blocking": [],
            "advisory": advisory,
            "coverage": {},
        }
        rec = _rec(
            changed_files=["a.py", "b.py"],
            context=_diff_context(50),
            verify_requests=0,  # low grounding
            dropped=[{"id": "d1"}, {"id": "d2"}],  # high drop-rate
            surfaced=advisory,
        )
        gate_dispatch._attach_code_review_metrics(verdict, rec, total_ms=1.0)
        assert verdict["verdict"] == original  # untouched
        assert verdict["blocking"] == []  # no blocking source added
        # the notes are advisory, on coverage, not the verdict
        assert "grounding_note" in verdict["coverage"]
        assert "approach_viability_note" in verdict["coverage"]
