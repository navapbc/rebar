"""Editor inspector contract-views (workflow authoring v2).

The read-only CONTRACT surfacing the visual editor shows for a selected node — a
scripted op's or an agent prompt's CONSUMES (input fields) / PRODUCES (output fields)
+ description, plus the per-node map the bundle looks up by element name. Split out of
``editor.py`` (its own cohesive concern: turning a step's declared contract into the
inspector's display shape), and re-exported from ``editor`` for the stable import
surface. Pure data assembly — no HTTP/server code; contracts/prompts/schemas are
imported lazily so this stays lean.
"""

from __future__ import annotations

from typing import Any

# The defined EMPTY/no-contract inspector state (workflow authoring v2, 5e78 AC): a
# step with no declared contract — or nothing selected — renders this, never a crash.
_EMPTY_CONTRACT_VIEW: dict[str, Any] = {
    "has_contract": False,
    # `checked` is the UI's "unchecked badge" signal (c768): False for an opaque /
    # contract-less node (nothing declared to statically check against), True once a
    # contract is present. Mirrors `has_contract` here but is a distinct, explicit
    # affordance the bundle renders as a visible "⚠ unchecked (opaque source)" label.
    "checked": False,
    "description": "",
    "consumes": [],
    "produces": [],
}


def _schema_fields(schema_name: str | None) -> list[dict[str, Any]]:
    """The flat field list (``{name, type, required, description}``) of a contract
    schema's top-level object ``properties``, for the inspector's CONSUMES/PRODUCES.
    Best-effort: an unreadable/non-object schema yields an empty list (never raises)."""
    if not schema_name:
        return []
    try:
        from rebar import schemas

        schema = schemas.load(schema_name)
    except Exception:  # noqa: BLE001 - an unresolvable contract surfaces as empty, not a crash
        return []
    props = schema.get("properties")
    if not isinstance(props, dict):
        return []
    required = set(schema.get("required") or [])
    fields: list[dict[str, Any]] = []
    for name, sub in props.items():
        sub = sub if isinstance(sub, dict) else {}
        typ = sub.get("type")
        if typ is None and "$ref" in sub:
            typ = "object"
        if isinstance(typ, list):
            typ = " | ".join(str(t) for t in typ)
        fields.append(
            {
                "name": name,
                "type": typ or "",
                "required": name in required,
                "description": sub.get("description", ""),
            }
        )
    return fields


def step_contract_view(uses: str | None) -> dict[str, Any]:
    """The editor inspector's read-only view of a scripted op's CONTRACT: its
    description plus CONSUMES (input fields) and PRODUCES (output fields). An op with
    no declared contract (or ``None``/unknown) yields the defined empty state, so the
    inspector always renders something (workflow authoring v2, 5e78)."""
    if not uses:
        return dict(_EMPTY_CONTRACT_VIEW)
    try:
        from .executor import contract_for

        contract = contract_for(uses)
    except Exception:  # noqa: BLE001 - registry trouble degrades to the empty state
        contract = None
    if contract is None:
        return dict(_EMPTY_CONTRACT_VIEW)
    return {
        "has_contract": True,
        "checked": True,
        "description": contract.description,
        "consumes": _schema_fields(contract.input_schema),
        "produces": _schema_fields(contract.output_schema),
    }


def prompt_contract_view(prompt_id: str | None, *, repo_root: Any = None) -> dict[str, Any]:
    """The inspector's read-only contract view of an AGENT step's prompt (story 4b2f):
    its ``description`` plus CONSUMES (``inputs``) / PRODUCES (``outputs``), built from
    the prompt front-matter. A prompt's ``inputs``/``outputs`` may be schema NAMES
    (resolved via ``rebar.schemas`` like the scripted path) or may be absent → the
    empty/no-contract state. Best-effort: an unresolvable/unknown prompt id degrades to
    the empty state, never raises."""
    if not prompt_id:
        return dict(_EMPTY_CONTRACT_VIEW)
    try:
        from rebar.llm.prompts import get_prompt

        prompt = get_prompt(prompt_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 - an unknown/malformed prompt surfaces as empty, not a crash
        return dict(_EMPTY_CONTRACT_VIEW)
    consumes = _schema_fields(prompt.inputs) if isinstance(prompt.inputs, str) else []
    produces = _schema_fields(prompt.outputs) if isinstance(prompt.outputs, str) else []
    if not (prompt.description or consumes or produces):
        return dict(_EMPTY_CONTRACT_VIEW)
    return {
        "has_contract": True,
        "checked": True,
        "description": prompt.description or "",
        "consumes": consumes,
        "produces": produces,
    }


def resolve_contracts(doc: dict[str, Any], *, repo_root: Any = None) -> dict[str, dict[str, Any]]:
    """Map each step's contract-bearing key to its read-only contract view, so the
    editor can surface a selected node's contract (keyed by the element ``name`` the
    bundle looks up): a SCRIPTED step's ``uses`` op name → :func:`step_contract_view`,
    and an AGENT step's ``prompt`` id → :func:`prompt_contract_view`."""
    out: dict[str, dict[str, Any]] = {}
    for s in doc.get("steps", []) or []:
        out.update(_contracts_in(s, repo_root=repo_root))
    return out


def _contracts_in(step: Any, *, repo_root: Any = None) -> dict[str, dict[str, Any]]:
    """Recurse a step (and any nested branch/loop/map frames) collecting the contract
    view of every scripted ``uses`` op AND every agent ``prompt`` id encountered."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(step, dict):
        return out
    uses = step.get("uses")
    if isinstance(uses, str) and uses and uses not in out:
        out[uses] = step_contract_view(uses)
    prompt_id = step.get("prompt")
    if isinstance(prompt_id, str) and prompt_id and prompt_id not in out:
        out[prompt_id] = prompt_contract_view(prompt_id, repo_root=repo_root)
    for block, *keys in (("loop", "body"), ("map", "body"), ("branch", "then", "else")):
        blk = step.get(block)
        if isinstance(blk, dict):
            for key in keys:
                for child in blk.get(key) or []:
                    out.update(_contracts_in(child, repo_root=repo_root))
    return out
