"""Canonical criterion-id → rubric-prompt-id mapping (task stew-kid-motif / epic 3156).

A criterion's LOGICAL id is namespaced: a built-in is a bare id (``F1``, ``T5a``); a project
criterion is ``project.<name>`` (dotted — the collision-safe namespace that guarantees a
project criterion can never rebind a built-in, ADR 0015). Its RUBRIC is a prompt-library file
whose id must be FILESYSTEM-SAFE — ``[A-Za-z0-9][A-Za-z0-9-]*`` (``prompt_authoring._valid_id``)
— because a ``.`` in ``.rebar/prompts/<id>.md`` collides with the ``<id>.<variant>.md`` overlay
convention (and ``_valid_id`` forbids it outright).

So the logical id is DECOUPLED from the physical prompt id via this deterministic,
FORWARD-ONLY, gate-qualified map — the pattern popular, actively-maintained tools use (Semgrep's
dotted rule ``id`` is metadata decoupled from the filename; npm maps ``@scope/name`` →
``node_modules/@scope/name``; Python maps ``a.b.c`` → ``a/b/c.py``). The default
``gate_key="plan_review"`` preserves the existing plan-review mapping; ``gate_key="code_review"``
uses the corresponding code-review prefix:

    plan_review built-in  ``F1``           → ``plan-review-F1``
    plan_review project   ``project.foo``  → ``plan-review-project-foo``
    code_review built-in  ``F1``           → ``code-review-F1``
    code_review project   ``project.foo``  → ``code-review-project-foo``

The map is TOTAL and INJECTIVE because a project ``<name>`` is constrained to the SAME charset as
any prompt id (``[A-Za-z0-9][A-Za-z0-9-]*`` — alnum + dash, NO dots/underscores; enforced by
``criteria.overlay._validate_routing_entry``), so the single namespace dot is the only ``.`` and
the ``.``→``-`` rewrite can never collide. It is used at BOTH the descriptor-resolution site
(``plan_review.registry``) and the editor-authoring site (``workflow.criterion_preview`` /
``editor``) so the two can never diverge. It is deliberately one-way: a name may contain dashes,
so the sanitized id is NOT reversibly split back to the dotted id — the dotted id is always
carried explicitly, never reverse-derived.
"""

from __future__ import annotations

from rebar.llm.criteria.model import CriteriaError

#: The prompt-library id prefix every plan-review criterion rubric carries.
PLAN_REVIEW_PROMPT_PREFIX = "plan-review-"
#: The prompt-library id prefix every code-review criterion rubric carries.
CODE_REVIEW_PROMPT_PREFIX = "code-review-"
#: Prompt-library prefixes keyed by their owning review gate.
_PROMPT_PREFIX = {
    "plan_review": PLAN_REVIEW_PROMPT_PREFIX,
    "code_review": CODE_REVIEW_PROMPT_PREFIX,
}
#: The dotted project-criterion namespace (mirrors ``criteria.overlay._PROJECT_PREFIX``).
PROJECT_PREFIX = "project."


def criterion_prompt_id(criterion_id: str, *, gate_key: str = "plan_review") -> str:
    """The filesystem-safe prompt-library id storing ``criterion_id``'s rubric.

    ``project.<name>`` → ``<gate>-project-<name>`` (the single namespace dot → ``-``);
    every other id → ``<gate>-<id>`` unchanged. Forward-only + injective given the project name
    charset (see module docstring)."""
    try:
        prefix = _PROMPT_PREFIX[gate_key]
    except KeyError as exc:
        raise CriteriaError(
            f"criterion_prompt_id: unknown gate {gate_key!r} "
            "(expected 'plan_review' or 'code_review')"
        ) from exc
    return f"{prefix}{criterion_id.replace('.', '-')}"
