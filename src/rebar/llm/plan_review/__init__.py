"""The plan-review gate (epic 5fd2 — ``messy-moose-jig``).

A plan-review verification gate that fires at open→in_progress — the inverse of the
completion-verifier close gate. It COACHES agents toward better plans (advisory by
default in v1) and emits a signed **attestation** that a review process was followed
(a composable "rigorous agentic development vs vibe-coding" CI signal).

Two surfaces:

* :func:`review_plan` — the heavy, out-of-band capability: run the three-pass
  review against a ticket's whole plan, emit the ``REVIEW_RESULT`` sidecar, and (on
  a non-blocking PASS) sign a plan-review attestation. CLI: ``rebar review-plan``;
  write-gated MCP: ``review_plan``.
* :func:`claim_gate_check` — the FAST, local check the ``claim`` path uses when the
  gate is enabled (``verify.require_plan_review_for_claim``): a pure HMAC verify +
  freshness/material binding, NO LLM and NO network. ``--force`` bypasses it.

Optionality: stdlib-only at import (the registry/DET tier are pure Python; the LLM
passes lazy-import the runner stack). ``review_plan`` needs the ``[agents]`` extra +
a model key only to run the LLM tiers; the DET floor + attestation work without it.
"""

from __future__ import annotations

from typing import Any

from rebar.llm.config import LLMConfig
from rebar.llm.runner import Runner

from . import attest, orchestrator, sidecar
from .attest import claim_gate_check

__all__ = ["review_plan", "claim_gate_check", "registry_coverage"]


def review_plan(
    ticket_id: str,
    *,
    repo_root=None,
    config: LLMConfig | None = None,
    runner: Runner | None = None,
    sign: bool = True,
    emit_sidecar: bool = True,
    advisory_cap: int | None = None,
) -> dict[str, Any]:
    """Run the plan-review gate on ``ticket_id`` and return a ``plan_review_verdict``.

    Assembles the whole-ticket context, runs the DET floor + the three-pass LLM
    review, mints finding ids, applies the advisory cap, runs the Pass-4 coach, emits
    the ``REVIEW_RESULT`` sidecar (best-effort), and — on a non-blocking ``PASS`` —
    signs a plan-review attestation (so a subsequent ``claim`` passes the gate).

    Returns ``{verdict: PASS|BLOCK|INDETERMINATE, ticket_id, ticket_type, blocking[],
    advisory[], coaching[], indeterminate[], coverage, signature?, ...}``. Bugs and
    session_logs are exempt (PASS, runner=exempt). Raises only on a hard
    context-assembly failure; an unavailable LLM degrades to a DET-only review.
    """
    cfg = config or LLMConfig.from_env(repo_root=repo_root)
    ctx = orchestrator.assemble_context(ticket_id, repo_root=repo_root, cfg=cfg)
    cap = advisory_cap if advisory_cap is not None else orchestrator.DEFAULT_ADVISORY_CAP
    verdict = orchestrator.run_review(ctx, cfg, runner=runner, advisory_cap=cap)

    material = orchestrator.material_fingerprint(ctx)
    verdict["material_fingerprint"] = material

    # Sidecar (best-effort; never fails the review). Skippable for a pure-read run.
    verdict["sidecar_emitted"] = (
        sidecar.emit(verdict, material=material, repo_root=repo_root) if emit_sidecar else False
    )

    # Sign on a non-blocking PASS (not for exempt/blocking/indeterminate). The
    # attestation = "process followed, no blocking red flags + coverage", NOT
    # "perfect"; advisory findings are coaching, not blocks.
    if sign and verdict.get("verdict") == "PASS" and verdict.get("runner") != "exempt":
        try:
            sig = attest.sign_plan_review(verdict, material=material, repo_root=repo_root)
            verdict["signature"] = {
                "signed": True,
                "key_id": sig.get("key_id"),
                "head_sha": sig.get("head_sha"),
            }
        except Exception as exc:  # surface, don't crash: a missing key is a real signal
            verdict["signature"] = {"signed": False, "error": str(exc)}
    else:
        verdict.setdefault("signature", {"signed": False, "reason": verdict.get("verdict")})

    # Validate the assembled verdict against its documented contract (shape-only,
    # permissive) — the same final re-validation the completion op does. Pins the
    # CLI/library `--output json` shape to plan_review_verdict.schema.json.
    from rebar.llm import findings

    return findings.validate_structured(verdict, "plan_review_verdict")


def registry_coverage() -> tuple[bool, list[str]]:
    """The criteria-registry completeness guard (re-exported for CI)."""
    from .registry import check_registry_coverage

    return check_registry_coverage()
