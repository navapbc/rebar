"""Typed configuration fields for verification gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeVar

_FieldValue = TypeVar("_FieldValue")


def _documented(default: _FieldValue, description: str) -> _FieldValue:
    """Build a dataclass field carrying its client-facing description."""
    return field(default=default, metadata={"public_description": description})


@dataclass
class VerifyConfig:
    # Gate-wide admission limit; historical p99 is 7,500 chars, so 8,000 protects only the tail.
    max_ticket_description_chars: int = _documented(
        8_000,
        "Sets the ticket description limit used by plan review and completion verification.",
    )
    # Opt-in material-pin enforcement; off for pre-feature project/attestation compatibility.
    enforce_plan_material_pins: bool = _documented(
        False,
        "Requires plan-review signatures to pin reviewed ticket material.",
    )
    # Opt-in completion-verification close gate: when true, closing a work ticket runs the
    # LLM completion-verifier (rebar.llm.verify_completion) and blocks on FAIL / unavailable
    # LLM (fail-closed; --force bypasses without signing). On PASS the verdict is signed.
    # Default off.
    # read-via: _commands/gates.py gate_enabled string key
    require_completion_verification_for_close: bool = _documented(
        False,
        "Requires a passing completion verification before a work ticket can close.",
    )
    # Opt-in local plan-review close gate. It verifies a separately attested review with the
    # CLOSE validity profile; it never launches an LLM review. Default off.
    # read-via: _commands/gates.py string key
    require_plan_review_for_close: bool = _documented(
        False,
        "Requires a current plan-review attestation when a work ticket closes.",
    )
    # Opt-in plan-review gate (epic 5fd2): when true, claiming a work ticket
    # (open→in_progress) requires a fresh, certified plan-review attestation (run
    # `rebar review-plan <id>` to earn one). Absent / stale (code-HEAD moved) /
    # material-edited signatures BLOCK the claim; `--force` bypasses with a logged
    # justification. A FAST local HMAC check only — no LLM on the claim path. Bugs
    # and session_logs are exempt. Default off ⇒ `claim` keeps today's behavior;
    # turning it off is the rollback (an ordinary preference, no kill-switch needed).
    # read-via: _commands/gates.py string key
    require_plan_review_for_claim: bool = _documented(
        False,
        "Requires a current passing plan-review attestation before a work ticket can be claimed.",
    )
    # Opt-in store-wide duplicate-ticket suggestions (epic only-crave-art). ADVISORY: adds an
    # overlap step (enrich → BM25F retrieve → pairwise judge) surfacing ≤3 duplicate/supersede/
    # dependency link suggestions under an `overlap[]` verdict key; NEVER blocks claim. Cost
    # scales with TRACKER SIZE, not the ticket. Off by default; tunables on LLMConfig
    # (`[tool.rebar.llm] overlap_*`). Pre-rename spelling stays honored — see `_ALIASES`.
    suggest_duplicate_tickets: bool = _documented(
        False,
        "Adds duplicate, supersession, and dependency suggestions plus recent-title warnings.",
    )
    # Opt-in commit-ticket gate: when true, `rebar verify-commit-ticket` (run in CI, the
    # Gerrit Verified leg) requires every commit message to reference a rebar ticket that
    # RESOLVES in the store (alias/full/short/Jira). Default off; enabled per-project in
    # rebar.toml. Turning it off is the rollback. See docs/commit-ticket-trailer.md.
    require_ticket_for_commit: bool = _documented(
        False,
        "Requires each checked commit to reference a ticket that resolves in the store.",
    )
    # Agentic code-review DISPATCH enablement (epic b744): consulted only by
    # `produce_code_review_verdict` callers that leave `enabled=None` (automated/gate-triggered
    # dispatch) — when false such a dispatch is INERT (zero LLM calls). An EXPLICIT
    # `review_code()` call (CLI `rebar review-code` / library / MCP `review_code`) always runs
    # the four-pass code-review GATE (`gates/code-review.yaml`) regardless of this key — an
    # explicit invocation is the caller's intent (bug 5b32-37c4-f99a-4315), mirroring
    # review-plan, where config controls requiredness, never availability.
    # Env override: REBAR_VERIFY_ENABLE_CODE_REVIEW.
    enable_code_review: bool = _documented(
        False,
        "Enables automatic code-review dispatch without disabling explicit review requests.",
    )
    # Progressive drift-refresh (Story 2, epic boil-golem-veto / ADR 0002): on a
    # drift-only-stale re-review, run a cheap E4+G1G2 probe and, if the plan still holds,
    # REFRESH the attestation instead of a full re-review. Always on (operator-authorized
    # 2026-07-12, epic a37b, on the measured token/latency saving); the off switch was
    # retired in story 4cdf.

    # Token-budget headroom for the Pass-2 verify chunker (epic solid-timer-unison WS3): the
    # fraction of the verifier model's context window a single verify request may use before
    # the findings are split into multiple calls. Default 0.8 leaves room for the system
    # prompt + the per-finding structured output. The common case (whole request fits) is one
    # aggregate call; this only triggers on a pathological huge-findings ticket.
    verify_window_headroom: float = _documented(
        0.8,
        "Limits each plan-review verification request to this fraction of the model window.",
    )

    # Convergent plan-edit re-review (epic 7d43, child ec89): a re-review of an EDITED plan whose
    # reviewed CODE is unchanged always runs in remediation mode — the full criteria set still
    # runs, but Pass-3 may drop only NOVEL, low-priority findings (the rising floor, child cc5b).
    # Always on (operator-authorized on field evidence, 2026-07-11); the off switch was retired
    # in story 4cdf.
    # The freshness window (minutes) for remediation mode: a re-review is eligible only when the
    # LAST review of any kind was within this many minutes, measured from that last review and
    # RESET on each review (so the loop persists across a series of edits and lapses to a normal
    # full review only after the agent goes idle). Default 60.
    remediation_window_minutes: int = _documented(
        60,
        "Sets how long an edited plan remains eligible for remediation-mode review.",
    )

    # Pass-3 rising floor (epic 7d43, child cc5b). On an eligible remediation re-review, a finding
    # is DROPPED iff its novelty >= novelty_drop_threshold AND its priority (validity × impact) <
    # novelty_priority_floor. T_novel default 0.7 (house precision-first). The floor is a scalar at
    # the corpus p40 impact percentile (~0.4, the "below major" band; see
    # scripts/plan_review_impact_distribution.py). Both config-overridable.
    novelty_drop_threshold: float = _documented(
        0.7,
        "Sets the novelty score required before remediation review may drop a finding.",
    )
    novelty_priority_floor: float = _documented(
        0.4,
        "Keeps remediation findings at or above this priority score even when they are novel.",
    )
    # The rising floor is always active (shared with the code-review region-gated floor, ADR 0037;
    # operator-authorized on field evidence, 2026-07-11, in lieu of 150b's `discriminates_novelty`
    # eval). It still runs subject to remediation eligibility + per-review self-gates; the off
    # switch (`novelty_drop_active`) was retired in story 4cdf.

    # Pass-3 COMPLETION floor (epic 66ac / story 6533) — the container-completion analogue of the
    # novelty rising floor, for a re-fired epic/story-with-children review. A finding is DROPPED iff
    # its completion sub-answers say it is fully about DELIVERED, settled plan text (attribution = a
    # delivered-now child AND containment = limited-to-closed AND layer = plan-semantics) AND its
    # priority (validity × impact) < completion_priority_floor AND none of its criteria is in the
    # always-preserve set. Every ambiguous/fail-safe sub-answer fails toward KEEP. The floor default
    # (0.4) matches novelty_priority_floor (the corpus "below major" band).
    completion_priority_floor: float = _documented(
        0.4,
        "Keeps completion findings at or above this priority when the floor is active.",
    )
    # The always-preserve set: REGISTERED criterion ids a completion drop never touches, regardless
    # of the other axes. Default the security overlay (T5c) + the endpoint/interface-contract
    # criterion (T10) — so a delivered child's "endpoint has no auth" or "contract omits a field"
    # is always kept. Adding privacy/compliance ids is a config change, not code.
    completion_preserve_criteria: tuple[str, ...] = _documented(
        ("T5c", "T10"),
        "Names criteria that completion review never drops through the completion floor.",
    )
    # The EVIDENCE GATE: the completion floor stays inert (gate runs un-floored) until this is
    # flipped true — manually by the operator only after the calibration gold-set (story 77cf) has
    # cleared its must-never-suppress bar. Default False, so the floor never drops a finding by
    # default (the total back-out).
    completion_floor_active: bool = _documented(
        False,
        "Enables the calibrated completion floor, which may drop eligible low-priority findings.",
    )
    # Completion-recovery banking + criteria-scaled step floor (epic 10ae, story 2948) — FLAT
    # completion_* fields (see verify_step_floor/plan_recovery_pool + docs; pool = multiplier × N).
    completion_recovery_pool_multiplier: float = _documented(
        1.5,
        "Scales the completion-verifier recovery step pool by the number of reviewed criteria.",
    )
    completion_verify_steps_per_criterion: int = _documented(
        24,
        "Adds this many completion-verifier steps for each reviewed criterion.",
    )
    completion_verify_step_floor_min: int = _documented(
        160,
        "Sets the minimum step budget for a completion-verification run.",
    )
    # Evidence-surface scaling terms (ticket 8d74): epic criteria traverse child tickets
    # (show_ticket + comments + repo reads each), and every run pays a fixed show_ticket+parse
    # overhead — both sized at 16 steps (mid-band of observed 5-10 requests/child at the
    # 2x steps→requests halving). Consumed by verify_step_floor's child-traversal + overhead
    # terms; 0 disables a term.
    completion_verify_child_traversal_steps: int = _documented(
        16,
        "Adds this many verifier steps for each child ticket traversed during completion review.",
    )
    completion_verify_fixed_overhead_steps: int = _documented(
        16,
        "Adds a fixed step allowance to every completion-verification run.",
    )
    # Bounded auto-resume for the completion-verification close gate (ticket b5f8): when a
    # close FAILs on pure evidence-search exhaustion (every unmet criterion carries the
    # framework-set per-criterion `evidence_sufficient: false` marker — nothing positively
    # refuted), the gate re-runs `verify_completion` itself instead of asking the operator to
    # retype the same close; the cross-run verdict cache seeds prior validated PASSes, so each
    # re-run concentrates its budget on the formerly-insufficient criteria. At most this many
    # resumptions per close invocation, and only while the prior attempt strictly increased
    # the cache-credited PASS count (zero progress stops early). 0 disables auto-resume.
    auto_resume_max: int = _documented(
        2,
        "Limits automatic completion-verification retries after evidence-search exhaustion.",
    )
    # Validation-assessment cross-checks (bug 5e40) — two per-verdict consistency drops that
    # converge a non-deterministic re-review. Each stays inert (the gate runs un-cross-checked, the
    # verdict byte-identical) until flipped true, mirroring completion_floor_active's evidence gate:
    # the mechanism ships off, an operator enables it only after calibration confirms it never
    # suppresses a real finding.
    #   - contradiction_xcheck_active: cross-check the verdict's findings for a MUTUAL contradiction
    #     and drop the contradicted/weaker one (5e40 A1: a false BLOCK refuted by a true advisory).
    #   - comment_trail_xcheck_active: consult the ticket's recorded comment trail and drop a
    #     finding that re-litigates a point the trail already RESOLVED (5e40 B3: rebase:chain).
    contradiction_xcheck_active: bool = _documented(
        False,
        "Enables a cross-check that drops a weaker finding contradicted by another finding.",
    )
    comment_trail_xcheck_active: bool = _documented(
        False,
        "Enables a cross-check that drops findings which repeat matters resolved in comments.",
    )
    # Opt-in per-gate required-signing-environment (story 42d1). When set to an env_id, a gate's
    # operation certificate must come from that pinned trusted environment
    # (`.rebar/trusted_environments.yaml`), verified against its out-of-band-pinned key. Default
    # None ⇒ no required environment (the low-security default).
    require_environment: str | None = _documented(
        None,
        "Restricts operation certificates to the trusted environment with this identifier.",
    )
    # Grandfathering boundary for the op-cert merge-gate (`rebar verify-opcert`, story 4214). A git
    # ref (commit/tag/branch) on the tracker branch: only tickets whose close-STATUS introducing
    # commit is `<ref>` or a descendant of it are ENFORCED; pre-existing (ancestor) closures are
    # reported but never fail the gate. Unset ⇒ every in-scope closed ticket is enforced (no
    # grandfathering). Overridable per-run by `rebar verify-opcert --since <ref>`. Mirrors
    # identity.enforce_since for the authorship gate.
    opcert_enforce_since: str | None = _documented(
        None,
        "Limits operation-certificate checks to closure commits at this ref or descendants.",
    )
    # Opt-in trusted op-cert gate service base URL (story ee0b). When set, `rebar remote-cert`
    # routes a gate run to the trusted environment at this URL (which fetches authoritative
    # state itself, runs the gate, and returns a signed op-cert). Unset ⇒ the remote path is
    # simply unavailable and `rebar remote-cert` errors with a clear message; it is NEVER
    # required for any LOCAL op-cert sign/verify path (those stay fully offline). Default None.
    opcert_remote_url: str | None = _documented(
        None,
        "Routes remote certificate requests to the trusted gate service at this base URL.",
    )
