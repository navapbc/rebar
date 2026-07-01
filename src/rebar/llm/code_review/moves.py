"""The Pass-4 code move-catalog for the code-review gate (epic b744 / WS2).

A ``MOVE_REGISTRY_SCHEMA`` instance (the kernel's move-catalog shape): each move is
``{name, template, applies_when?}`` and renders coaching DETERMINISTICALLY from its template
(the kernel ``coach()`` substitutes ``{subject}``; the LLM only picks the move + names the
subject). ``applies_when`` tags are drawn from the closed ``{OVERLAY_IDS ∪ "always"}``
vocabulary; the gate passes ``active_triggers`` = the union of ``criteria`` carried by the
surviving findings, and the kernel offers only moves whose ``applies_when`` overlaps them (or is
empty / ``["always"]``). Validated through the kernel schema at load.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rebar.llm.review_kernel import validate_move_registry

# Built-in code moves. Each template carries a single ``{subject}`` placeholder.
MOVE_REGISTRY: dict[str, dict[str, Any]] = {
    "extract-helper": {
        "name": "extract a shared helper",
        "template": "Extract the duplicated logic in {subject} into a shared helper.",
        "applies_when": ["always"],
    },
    "add-regression-test": {
        "name": "add a regression test",
        "template": "Add a regression test covering {subject}.",
        "applies_when": ["tests", "always"],
    },
    "threat-model": {
        "name": "threat-model the change",
        "template": "Threat-model the change to {subject} (trust boundary, input validation).",
        "applies_when": ["security"],
    },
    "add-migration-guard": {
        "name": "guard the migration",
        "template": "Guard the migration in {subject}: expand-contract, reversible, backfill-safe.",
        "applies_when": ["db-migrations"],
    },
    "bound-the-hot-path": {
        "name": "bound the hot path",
        "template": "Bound or memoize the hot path in {subject}.",
        "applies_when": ["performance"],
    },
    "check-api-compat": {
        "name": "check API back-compat",
        "template": "Confirm {subject} preserves backward compatibility (or version/deprecate it).",
        "applies_when": ["api-compat"],
    },
    "update-docs": {
        "name": "update the docs",
        "template": "Update the user/operator/API docs that track {subject}.",
        "applies_when": ["docs", "always"],
    },
}


def load_move_registry(repo_root: str | None = None) -> dict[str, dict[str, Any]]:
    """The Pass-4 code move-registry INSTANCE the gate supplies to the kernel ``coach()``:
    the built-in :data:`MOVE_REGISTRY` PLUS project extensions from
    ``.rebar/code_review_moves.json`` (a ``{move_id: {name, template, applies_when?}}`` map; a
    project entry adds or overrides by id). Built-ins validated STRICTLY (a bad move raises at
    load); the project file best-effort (``strict=False`` — a malformed entry is DROPPED, the
    review never crashes). Mirrors ``plan_review.passes.load_move_registry``."""
    moves = validate_move_registry({mid: dict(m) for mid, m in MOVE_REGISTRY.items()})
    if not repo_root:
        return moves
    try:
        path = Path(repo_root) / ".rebar" / "code_review_moves.json"
        if path.is_file():
            extra = json.loads(path.read_text(encoding="utf-8"))
            moves.update(validate_move_registry(extra or {}, strict=False))
    except Exception:  # noqa: BLE001 — project move file is best-effort
        pass
    return moves
