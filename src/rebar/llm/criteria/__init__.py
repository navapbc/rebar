"""``rebar.llm.criteria`` — the SHARED criteria layer both review gates delegate to
(story 5065, the capstone of epic 3156's cross-gate unification).

Plan-review (``rebar.llm.plan_review.registry``) and code-review
(``rebar.llm.code_review.registry``) keep their public functions; those now DELEGATE to
this one shared implementation instead of each carrying a private copy. Two pieces of
machinery live here:

* :func:`threshold_for` — the reconciled ``(block_threshold, blocking)`` resolver that hosts
  BOTH gates' blocking conventions side-by-side, dispatched on ``gate=`` (``plan_review``
  blocks on ``default_posture``; ``code_review`` on ``blocking_enabled``). The divergence is
  DELIBERATE and preserved (see ADR 0017), not collapsed.
* :func:`build_descriptor` — the exec-tier-polymorphic descriptor builder (a prompt-less DET
  descriptor vs a prompt-resolved LLM descriptor), generalizing plan-review's
  ``_descriptor_from_prompt``.
* the overlay core (:func:`effective_routing` / :func:`effective_criteria` /
  :func:`disabled_builtins`, gate-parameterized via :func:`register_gate`) — the
  ``.rebar/criteria_routing.json`` merge / activation / cache-isolation logic, generalized
  from plan-review's ef7e so code-review gains overlay support reading its own ``code_review``
  gate key from the SAME file.

Delegation, not rip-and-replace: an overlay-absent repo behaves byte-identically to before.
"""

from __future__ import annotations

from .model import (
    DEFAULT_BLOCK_THRESHOLD,
    CriteriaError,
    build_descriptor,
    threshold_for,
)
from .overlay import (
    clear_caches,
    disabled_builtins,
    effective_criteria,
    effective_routing,
    register_gate,
)

__all__ = [
    "DEFAULT_BLOCK_THRESHOLD",
    "CriteriaError",
    "build_descriptor",
    "threshold_for",
    "clear_caches",
    "disabled_builtins",
    "effective_criteria",
    "effective_routing",
    "register_gate",
]
