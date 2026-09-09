"""Map logical criterion ids to filesystem-safe, gate-qualified prompt ids.

Built-ins use bare logical ids. Project criteria use the collision-safe ``project.<name>``
namespace. Prompt ids permit ``[A-Za-z0-9][A-Za-z0-9-]*``, so the mapping replaces the sole
namespace dot and prepends the plan-review or code-review gate prefix.

The project-name grammar makes this forward mapping total and injective. Descriptor resolution
and prompt authoring share it. Prompt ids are never reverse-derived because project names may
contain hyphens.
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
