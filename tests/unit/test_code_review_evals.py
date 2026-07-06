"""Standing eval-suite tests for the CODE-REVIEW prompts (story f93a).

These validate the SHIPPED code-review eval specs offline (no model, no network): each
spec parses + passes ``validate_eval_spec`` (strict), and the dedicated ``code_review_*``
scorers are registered and behave. They also exercise the ``eval_solver.run_case``
code-review arm with a ``FakeRunner`` (offline) to prove it returns the prompt's NATIVE
structured output ({findings}/{verifications}), never a full-gate verdict. The live
recall/false-fire SCORING runs in the eval CI ([eval] extra + credentials).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar.llm.evals import eval as ev
from rebar.llm.evals import eval_scorers as sc
from rebar.llm.evals import eval_solver

CODE_REVIEW_SPECS = (
    "code-review-base",
    "code-review-verify",
    "code-review-tests",
    "code-review-security",
)

_REPO_ROOT = str(Path(__file__).resolve().parents[2])


# ── spec discipline ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("prompt_id", CODE_REVIEW_SPECS)
def test_code_review_spec_is_strict_clean(prompt_id: str) -> None:
    spec = ev.load_eval_spec(prompt_id)  # lenient load (raises on invalid)
    assert spec["prompt"] == prompt_id
    assert ev.validate_eval_spec(spec, strict=True) == [], prompt_id


@pytest.mark.parametrize("prompt_id", CODE_REVIEW_SPECS)
def test_code_review_spec_gating_scorers_registered(prompt_id: str) -> None:
    spec = ev.load_eval_spec(prompt_id)
    known = sc.known_scorer_names()
    det = [s for s in spec["scorers"] if s.get("type") == "deterministic"]
    assert det, f"{prompt_id} has no deterministic gating scorer"
    for s in det:
        assert s["name"] in known, f"{prompt_id}: unregistered scorer {s['name']!r}"


@pytest.mark.parametrize("prompt_id", CODE_REVIEW_SPECS)
def test_code_review_dataset_balanced(prompt_id: str) -> None:
    ds = ev.load_eval_spec(prompt_id).get("dataset", [])
    assert ds, f"{prompt_id} has no dataset"
    expects = {c.get("expect") for c in ds}
    assert expects & sc.FIRE_EXPECTS, f"{prompt_id} needs a recall (fire) case"
    assert expects & sc.NOFIRE_EXPECTS, f"{prompt_id} needs a no-fire (pass) case"
    # Every case carries a `diff` payload (the unified-diff text under review).
    assert all(c.get("diff") for c in ds), f"{prompt_id} case missing a `diff` payload"


def test_base_spec_encodes_nit_framing_baseline() -> None:
    # The nit-framing experiment: a committed no-fire case whose diff is ONLY style/format/
    # import nits (which linters enforce) — the base reviewer must NOT fire on it (baseline).
    ds = ev.load_eval_spec("code-review-base").get("dataset", [])
    nit = [c for c in ds if c.get("mode") == "nit-only-style"]
    assert nit, "base spec needs a nit-framing (nit-only-style) no-fire case"
    assert all(c.get("expect") == "pass" for c in nit)


# ── the dedicated code_review_* scorers ────────────────────────────────────────

_FINDINGS = {"findings": [{"claim": "x", "dimension": "bug", "evidence": "e"}]}
_EMPTY = {"findings": []}
_VERIFS = {"verifications": [{"index": 0, "binary": {"is_verifiable": "yes"}}]}


def test_code_review_scorers_registered() -> None:
    known = sc.known_scorer_names()
    for name in (
        "code_review_emits_valid_findings",
        "code_review_recall",
        "code_review_no_fire",
        "code_review_verify_emits_verifications",
    ):
        assert name in known, name


def test_code_review_emits_valid_findings() -> None:
    assert sc.score("code_review_emits_valid_findings", {}, _FINDINGS).passed is True
    # empty findings is still valid SHAPE (a shape check, not a fire check)
    assert sc.score("code_review_emits_valid_findings", {}, _EMPTY).passed is True
    bad = sc.score("code_review_emits_valid_findings", {}, {"nope": 1})
    assert bad.applicable is True and bad.passed is False


def test_code_review_recall_fires_on_findings() -> None:
    r = sc.score("code_review_recall", {"expect": "finding"}, _FINDINGS)
    assert r.applicable is True and r.passed is True
    miss = sc.score("code_review_recall", {"expect": "finding"}, _EMPTY)
    assert miss.applicable is True and miss.passed is False
    # not a should-fire case → excluded from recall
    assert sc.score("code_review_recall", {"expect": "pass"}, _EMPTY).applicable is False


def test_code_review_no_fire_on_clean_output() -> None:
    ok = sc.score("code_review_no_fire", {"expect": "pass"}, _EMPTY)
    assert ok.applicable is True and ok.passed is True
    false_fire = sc.score("code_review_no_fire", {"expect": "pass"}, _FINDINGS)
    assert false_fire.applicable is True and false_fire.passed is False
    assert sc.score("code_review_no_fire", {"expect": "finding"}, _FINDINGS).applicable is False


def test_code_review_verify_emits_verifications() -> None:
    ok = sc.score("code_review_verify_emits_verifications", {}, _VERIFS)
    assert ok.applicable is True and ok.passed is True
    empty = sc.score("code_review_verify_emits_verifications", {}, {"verifications": []})
    assert empty.applicable is True and empty.passed is False
    missing = sc.score("code_review_verify_emits_verifications", {}, _FINDINGS)
    assert missing.applicable is True and missing.passed is False


def test_code_review_case_never_reads_as_fail_verdict() -> None:
    # A {findings} output has NO `verdict` key, so `_fired` scores it by findings presence —
    # a code-review recall case can never vacuously pass through the FAIL-only path.
    assert sc._fired(_FINDINGS) is True
    assert sc._fired(_EMPTY) is False


# ── the run_case code-review arm (offline, FakeRunner — NO live call) ──────────


def test_run_case_base_arm_returns_native_findings() -> None:
    from rebar.llm.runner import FakeRunner

    payload = {"findings": [{"claim": "dangling ref"}], "recommend_overlays": []}
    out = eval_solver.run_case(
        "code-review-base",
        {"id": "t", "diff": "--- a\n+++ b\n@@\n-def f(): ...\n"},
        runner=FakeRunner(structured=payload),
        repo_root=_REPO_ROOT,
    )
    assert isinstance(out, dict) and isinstance(out.get("findings"), list)
    assert "verdict" not in out  # native output, NOT a full-gate verdict


def test_run_case_verify_arm_returns_verifications() -> None:
    from rebar.llm.runner import FakeRunner

    payload = {"verifications": [{"index": 0}]}
    out = eval_solver.run_case(
        "code-review-verify",
        {"id": "t", "diff": "--- a\n+++ b\n@@\n-x\n+y\n"},
        runner=FakeRunner(structured=payload),
        repo_root=_REPO_ROOT,
    )
    assert isinstance(out.get("verifications"), list) and out["verifications"]


def test_run_case_overlay_arm_returns_native_findings() -> None:
    from rebar.llm.runner import FakeRunner

    payload = {"findings": [{"claim": "sql injection"}]}
    out = eval_solver.run_case(
        "code-review-security",
        {"id": "t", "diff": "--- a\n+++ b\n@@\n+cur.execute('...' + user)\n"},
        runner=FakeRunner(structured=payload),
        repo_root=_REPO_ROOT,
    )
    assert isinstance(out.get("findings"), list)


def test_run_case_unknown_prompt_still_raises() -> None:
    from rebar.llm.runner import FakeRunner

    with pytest.raises(ValueError):
        eval_solver.run_case(
            "code-review-not-a-real-overlay",
            {"id": "t", "diff": "x"},
            runner=FakeRunner(structured={"findings": []}),
            repo_root=_REPO_ROOT,
        )
