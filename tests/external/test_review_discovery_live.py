"""RP-06 S7 — the live external-provider canary for the cross-gate discovery cutover (AC7).

[operator-attested] This is the ONLY test in the story that makes a real, billable model
call. It is inert by default (the external tier's ``REBAR_RUN_EXTERNAL`` opt-in) and skips —
visibly, never silently — unless the CONFIGURED provider's credential is present
(``_live_llm``). The orchestrator runs it against the live external-provider matrix; the
authoring session never executes it live.

What it proves that no offline test can: that a plan-review run drives at least ONE real
discovery call to the configured provider and still returns the NARROW public verdict, with
the reducer-ignored internal journal retained off that public surface.

Safety: it asserts only on booleans / enum verdicts / schema-shape. It NEVER prints the model
credential, the raw plan/context body, the prompt, or the internal trace — a live canary must
not leak sensitive data into CI logs (the RP-06 sensitive-data boundary).
"""

from __future__ import annotations

from pathlib import Path

import _live_llm
import pytest

import rebar
from rebar import schemas

pytestmark = pytest.mark.external

# Auto-marks this module's tests ``llm_live`` (tests/external/conftest.py) and feeds the
# all-skip canary, so an arm with no credential cannot report green having called no model.
_live_llm_ready = _live_llm.live_llm_ready()

_skip = _live_llm.skip_without_live_llm


@_skip
def test_live_plan_review_discovery_reaches_the_provider_and_stays_narrow(
    rebar_repo: Path,
    plan_review_fixture_plan: str,
) -> None:
    """A live plan-review run: ≥1 discovery call reaches the configured provider, the public
    verdict is REAL (PASS/BLOCK, not the INDETERMINATE a dead LLM path degrades to) and NARROW
    (conforms to plan_review_verdict), and the internal journal sidecar is retained."""
    import rebar.llm as llm

    ticket = rebar.create_ticket(
        "story",
        "Persist the review cache to disk",
        description=plan_review_fixture_plan,
        repo_root=str(rebar_repo),
    )

    # sign=False keeps the canary side-effect-free on the tickets store; emit_sidecar=True so
    # the reducer-ignored internal journal (the discovery trace) is actually written.
    verdict = llm.review_plan(ticket, repo_root=str(rebar_repo), sign=False, emit_sidecar=True)

    coverage = verdict.get("coverage", {})
    # ≥1 discovery call actually reached the live provider (not a DET short-circuit / stub).
    assert coverage.get("llm_ran") is True
    assert coverage.get("llm_unavailable") is not True
    # A real model verdict, never the degraded INDETERMINATE.
    assert verdict["verdict"] in ("PASS", "BLOCK")
    # The public surface stays NARROW: it conforms to the pinned public schema.
    schemas.validator(schemas.PLAN_REVIEW_VERDICT).validate(verdict)
    # The internal journal (the reducer-ignored discovery/usage trace) was retained off the
    # public verdict — observable via the sidecar-emitted flag, without exposing the trace.
    assert verdict.get("sidecar_emitted") is True
