"""The step-kind I/O CONTRACT model + the static compatibility check.

Extracted from ``executor.py`` along an existing call-graph seam: this cluster
(:class:`StepContract` / ``STEP_CONTRACTS`` / :func:`contract_for` /
:func:`input_schema_for` and the 3-state :func:`shallow_contract_check`) calls only
itself, and the executor's RUN LOOP never calls any of it. Its consumers are the
STATIC / edit-time surface — the editor inspector (:mod:`.editor`,
:mod:`.editor_contracts`), the reference linter (:mod:`.lint_refs`), and the
interpreter's pre-dispatch consumer-input validation — so it belongs beside them, not
inside the executor's control flow.

The dependency runs ONE WAY: :mod:`.executor` imports this module (``register_step``
writes into ``STEP_CONTRACTS``) and re-exports :class:`StepContract`,
``STEP_CONTRACTS``, :func:`contract_for` and :func:`shallow_contract_check`, so every
existing ``rebar.llm.workflow.executor`` import point keeps resolving. This module
imports nothing from ``executor``, so there is no cycle. Same shape as the two earlier
extractions out of that file, :mod:`.recorder` and :mod:`.runners`.

``STEP_REGISTRY``/``register_step``/``StepContext`` deliberately stay in
:mod:`.executor`: they are read as MODULE ATTRIBUTES across the test suite, and the
registry is the run loop's dispatch table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StepContract:
    """A step kind's authored I/O CONTRACT (workflow authoring v2, 5e78).

    Every step kind should advertise an INPUT contract, an OUTPUT contract, and a
    description so the editor can present a typed palette + inspector and the linter
    can check a `${{ steps.<id>.outputs.<name> }}` reference against the producing
    step's declared outputs. ``input_schema`` / ``output_schema`` are SCHEMA NAMES
    (resolvable via :mod:`rebar.schemas`), not inline schemas; either may be ``None``
    for a step whose I/O is not yet annotated (the linter then treats that step's
    outputs as UNKNOWN and never flags a reference to them)."""

    input_schema: str | None = None
    output_schema: str | None = None
    description: str = ""


# The contracts registered alongside scripted steps. Keyed by the same `uses` name as
# STEP_REGISTRY; a name absent here is a step with no declared contract (UNKNOWN).
STEP_CONTRACTS: dict[str, StepContract] = {}


def contract_for(step_name: str) -> StepContract | None:
    """The declared :class:`StepContract` for a scripted step ``name``, or ``None``
    when the step is unregistered or declares no contract (UNKNOWN to the linter)."""
    return STEP_CONTRACTS.get(step_name)


def input_schema_for(kind: str, action: str | None, repo_root: Any = None) -> str | None:
    """The INPUT-contract schema NAME a leaf step's resolved ``with`` must satisfy:
    scripted (``action`` = the ``uses`` op) → ``contract_for(action).input_schema``;
    agent (``action`` = the ``prompt`` id) → the prompt's front-matter ``inputs`` (when
    a schema name). ``None`` for any other kind, a missing action, or a contract-less
    step (UNKNOWN → validation is skipped, never failed).

    The SINGLE resolver shared by the runtime validator (interpreter) and the
    edit-time validator (editor) so the two can never diverge (story b642)."""
    if not isinstance(action, str) or not action:
        return None
    if kind == "scripted":
        try:
            # importing the step library populates STEP_CONTRACTS (else an empty
            # registry would false-pass when no one has imported it yet).
            from . import steps  # noqa: F401  (side effect: register contracts)

            contract = contract_for(action)
        except Exception:  # noqa: BLE001 - registry trouble → nothing to validate against
            return None
        return contract.input_schema if contract is not None else None
    if kind == "agent":
        try:
            from rebar.llm.prompting.prompts import get_prompt

            prompt = get_prompt(action, repo_root=repo_root)
        except Exception:  # noqa: BLE001 - unknown/malformed prompt → UNKNOWN (skip)
            return None
        return prompt.inputs if isinstance(prompt.inputs, str) else None
    return None


# ── 3-state SHALLOW structural compatibility check (port of spike E2, c768) ───
# Lives here (not lint_refs.py, which is at its size cap) beside the contract model
# it reasons over. A deliberately small STRUCTURAL check — NOT a subsumption engine.

_SHALLOW_COMBINATORS = ("oneOf", "anyOf", "allOf", "not")


def _schema_is_opaque(schema: Any) -> bool:
    """A top-level schema is OPAQUE (the shallow check abstains → UNKNOWN) when it is
    not a plain ``object`` schema with ``properties`` — it uses a combinator
    (``oneOf``/``anyOf``/``allOf``/``not``), is a bare ``$ref``, or simply lacks the
    object+``properties`` shape the shallow check reasons about. No subsumption is
    attempted; anything outside the simple object shape is UNKNOWN."""
    if not isinstance(schema, dict):
        return True
    if any(k in schema for k in _SHALLOW_COMBINATORS) or "$ref" in schema:
        return True
    return not isinstance(schema.get("properties"), dict)


def _field_is_opaque(sub: Any) -> bool:
    """A single property's subschema is opaque (UNKNOWN for that field, never an
    ERROR) when it is a combinator/``$ref`` or not a plain mapping."""
    if not isinstance(sub, dict):
        return True
    return any(k in sub for k in _SHALLOW_COMBINATORS) or "$ref" in sub


def _primitive_kinds(sub: dict[str, Any]) -> set[str] | None:
    """A property's declared primitive ``type`` as a set, or ``None`` when no ``type``
    is given (UNKNOWN for compatibility — never an ERROR). A ``type`` list is the set
    of its members."""
    typ = sub.get("type")
    if typ is None:
        return None
    if isinstance(typ, list):
        return {str(t) for t in typ}
    return {str(typ)}


def shallow_contract_check(source: dict, target: dict) -> str:
    """3-state SHALLOW structural compatibility of a producer's OUTPUT schema
    (``source``) against a consumer's INPUT schema (``target``). Returns exactly
    ``"OK"``, ``"UNKNOWN"`` (abstain), or ``"ERROR"``.

    A STANDALONE static building block (c768): the ENFORCED net is the runtime
    consumer-input check in :mod:`.interpreter`; this is the cheap edit-time/static
    counterpart. Lives here beside :class:`StepContract`/:func:`contract_for`.

    A deliberately small STRUCTURAL check, NOT a subsumption engine:

    * **UNKNOWN** if either schema is opaque at the top level (a combinator
      ``oneOf``/``anyOf``/``allOf``/``not``, a bare ``$ref``, or anything that is not a
      plain ``object`` schema with ``properties``). A single property whose subschema
      is a combinator/``$ref`` or carries no ``type`` is likewise treated as UNKNOWN
      *for that field* (never an ERROR).
    * **ERROR** if a ``target.required`` field is ABSENT from ``source.properties``, or
      a property present in BOTH declares incompatible primitive ``type``s (the type
      sets do not intersect). A ``type`` list is compatible if the sets intersect.
    * **OK** otherwise — every required target field is present with a compatible (or
      unknown) primitive kind.
    """
    if _schema_is_opaque(source) or _schema_is_opaque(target):
        return "UNKNOWN"
    src_props = source["properties"]
    tgt_props = target["properties"]
    required = target.get("required") or []
    if isinstance(required, list):
        for name in required:
            if isinstance(name, str) and name not in src_props:
                return "ERROR"
    for name, tgt_sub in tgt_props.items():
        if name not in src_props:
            continue
        src_sub = src_props[name]
        if _field_is_opaque(src_sub) or _field_is_opaque(tgt_sub):
            continue  # UNKNOWN for this field — never an ERROR
        src_kinds, tgt_kinds = _primitive_kinds(src_sub), _primitive_kinds(tgt_sub)
        if src_kinds is None or tgt_kinds is None:
            continue  # a missing type is UNKNOWN for this field, not an ERROR
        if not (src_kinds & tgt_kinds):
            return "ERROR"
    return "OK"


__all__ = [
    "STEP_CONTRACTS",
    "StepContract",
    "contract_for",
    "input_schema_for",
    "shallow_contract_check",
]
