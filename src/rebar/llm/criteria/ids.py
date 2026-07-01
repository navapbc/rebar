"""Canonical criterion-id → rubric-prompt-id mapping (task stew-kid-motif / epic 3156).

A criterion's LOGICAL id is namespaced: a built-in is a bare id (``F1``, ``T5a``); a project
criterion is ``project.<name>`` (dotted — the collision-safe namespace that guarantees a
project criterion can never rebind a built-in, ADR 0015). Its RUBRIC is a prompt-library file
whose id must be FILESYSTEM-SAFE — ``[A-Za-z0-9][A-Za-z0-9-]*`` (``prompt_authoring._valid_id``)
— because a ``.`` in ``.rebar/prompts/<id>.md`` collides with the ``<id>.<variant>.md`` overlay
convention (and ``_valid_id`` forbids it outright).

So the logical id is DECOUPLED from the physical prompt id via this single deterministic,
FORWARD-ONLY map — the pattern popular, actively-maintained tools use (Semgrep's dotted rule
``id`` is metadata decoupled from the filename; npm maps ``@scope/name`` →
``node_modules/@scope/name``; Python maps ``a.b.c`` → ``a/b/c.py``):

    built-in  ``F1``           → ``plan-review-F1``           (unchanged)
    project   ``project.foo``  → ``plan-review-project-foo``  (the one namespace dot → ``-``)

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

#: The prompt-library id prefix every plan-review criterion rubric carries.
PLAN_REVIEW_PROMPT_PREFIX = "plan-review-"
#: The dotted project-criterion namespace (mirrors ``criteria.overlay._PROJECT_PREFIX``).
PROJECT_PREFIX = "project."


def criterion_prompt_id(criterion_id: str) -> str:
    """The filesystem-safe prompt-library id storing ``criterion_id``'s rubric.

    ``project.<name>`` → ``plan-review-project-<name>`` (the single namespace dot → ``-``);
    every other id → ``plan-review-<id>`` unchanged. Forward-only + injective given the project
    name charset (see module docstring)."""
    return f"{PLAN_REVIEW_PROMPT_PREFIX}{criterion_id.replace('.', '-')}"
