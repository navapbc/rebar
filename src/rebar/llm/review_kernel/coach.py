"""Pass-4 of the four-pass review framework: the affirmative COACH (epic ``vivid-gang-day`` WS3).

Pass-4 maps each SURVIVING (advisory) finding to a move from a locked registry and renders
affirmative coaching DETERMINISTICALLY from the move's template — the LLM only picks a
``move_id`` + a bounded noun-phrase ``subject`` (validated); it NEVER authors free prose.

The MECHANISM is generic and lives HERE; the move CATALOG is domain-specific and is supplied
by each gate (plan-review's planning moves — spike, pre-mortem, ADR…; code review will supply
code moves). The kernel owns:

* the move-registry SCHEMA — ``{name, template, applies_when?}`` — and its load-time
  validation (:func:`validate_move_registry`). ``template`` must carry a single ``{subject}``
  placeholder (the LLM never authors prose); ``applies_when`` is a declarative list of trigger
  TAGS (absent / empty / ``["always"]`` ⇒ always applicable);
* the DETERMINISTIC applicability filter (:func:`applicable_moves`) — closing the prior gap
  where move selection was wholly LLM-delegated: the LLM now picks among ONLY the applicable
  subset;
* the surviving-findings LISTING, the deterministic RENDER, and the load-bearing SUBJECT
  VALIDATOR (a bounded noun-phrase: no code tokens, no leading imperative);
* :func:`coach` — the entry: gate-on-surviving>0 → applicability filter → the LLM ``pick``
  (injected seam) among the applicable moves → deterministic render. A pick OUTSIDE the
  applicable set is dropped at render, so the LLM can never select outside it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

# A move registry is ``{move_id: {name, template, applies_when?}}``.
MoveRegistry = dict[str, dict[str, Any]]

# The move-registry SCHEMA (the load-time validated contract every gate's catalog conforms to).
# A documented JSON-Schema-shaped description; :func:`validate_move_registry` enforces it.
MOVE_REGISTRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "A map of move_id -> move. Each move renders coaching deterministically.",
    "additionalProperties": {
        "type": "object",
        "required": ["name", "template"],
        "properties": {
            "name": {"type": "string", "description": "Short human name of the move."},
            "template": {
                "type": "string",
                "description": "Coaching template with a single {subject} placeholder.",
            },
            "applies_when": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Trigger tags gating applicability. Absent/empty or ['always'] => the move "
                    "is always applicable; otherwise applicable iff it intersects the active "
                    "triggers passed to coach()/applicable_moves()."
                ),
            },
        },
    },
}

ALWAYS = "always"


def validate_move_registry(registry: MoveRegistry, *, strict: bool = True) -> MoveRegistry:
    """Validate + normalize a move registry against :data:`MOVE_REGISTRY_SCHEMA`. Each move
    must carry a non-empty ``name`` and a ``template`` containing a single ``{subject}``
    placeholder; ``applies_when`` (if present) must be a list of string tags.

    ``strict=True`` (programmatic registries) RAISES ``ValueError`` on an invalid move so a
    gate author learns at load time. ``strict=False`` (best-effort project files) DROPS an
    invalid move instead of raising, so a malformed project move file never crashes a review.
    Returns the normalized registry (``applies_when`` defaulted to ``[]`` = always-applicable)."""
    out: MoveRegistry = {}
    for mid, move in (registry or {}).items():
        try:
            if not isinstance(move, dict):
                raise ValueError(f"move {mid!r} is not an object")
            name = move.get("name")
            template = move.get("template")
            if not name or not isinstance(name, str):
                raise ValueError(f"move {mid!r} missing a non-empty 'name'")
            if not isinstance(template, str) or "{subject}" not in template:
                raise ValueError(f"move {mid!r} template must contain a single '{{subject}}'")
            applies_when = move.get("applies_when", []) or []
            if not isinstance(applies_when, list) or not all(
                isinstance(t, str) for t in applies_when
            ):
                raise ValueError(f"move {mid!r} applies_when must be a list of string tags")
        except ValueError:
            if strict:
                raise
            continue  # best-effort: drop the malformed move, keep the review running
        norm: dict[str, Any] = {"name": name, "template": template}
        if applies_when:
            norm["applies_when"] = list(applies_when)
        out[str(mid)] = norm
    return out


def move_applies(move: dict[str, Any], active_triggers: Iterable[str]) -> bool:
    """Is ``move`` applicable given the active trigger tags? A move with no ``applies_when``
    (or ``["always"]``) is ALWAYS applicable; otherwise it is applicable iff its
    ``applies_when`` intersects ``active_triggers``."""
    aw = move.get("applies_when") or []
    if not aw or ALWAYS in aw:
        return True
    return bool(set(aw) & set(active_triggers))


def applicable_moves(registry: MoveRegistry, active_triggers: Iterable[str]) -> MoveRegistry:
    """The DETERMINISTIC applicability filter: the subset of ``registry`` whose moves apply
    given ``active_triggers``. The LLM then picks among ONLY this subset."""
    triggers = set(active_triggers)
    return {mid: m for mid, m in registry.items() if move_applies(m, triggers)}


def coach_listing(surviving: list[dict[str, Any]], registry: MoveRegistry) -> str:
    """The Pass-4 coach INSTRUCTIONS (the move registry + the surviving-findings listing).
    The ONE canonical format every gate's coach prompt consumes. ``registry`` is the ALREADY
    FILTERED applicable subset, so the LLM only ever sees moves it may pick."""
    listing = "\n".join(f"- id={f['id']} :: {f['finding'][:200]}" for f in surviving)
    moves = "\n".join(f"  {mid}: {m['name']}" for mid, m in sorted(registry.items()))
    return (
        f"## Move registry\n{moves}\n\n## Surviving findings (by id)\n{listing}\n\n"
        "Emit one note per finding you can map to a useful move (skip findings no move fits)."
    )


_IMPERATIVE_STARTS = (
    "add",
    "remove",
    "use",
    "create",
    "run",
    "implement",
    "write",
    "fix",
    "change",
    "delete",
    "refactor",
    "call",
    "set",
    "make",
    "update",
    "replace",
)


def validate_subject(subject: str) -> str | None:
    """The SUBJECT VALIDATOR (the load-bearing enforcement): a bounded noun-phrase — ≤8 words
    / ≤60 chars, no code tokens, not a leading imperative. Returns the cleaned subject or None
    (reject ⇒ no coaching for that finding; the LLM never authors prose)."""
    s = (subject or "").strip()
    if not s or len(s) > 60 or len(s.split()) > 8:
        return None
    if any(tok in s for tok in ("(", ")", "{", "}", ";", "=", "`", "()", "import ")):
        return None
    if s.split()[0].lower().rstrip(":,.") in _IMPERATIVE_STARTS:
        return None
    return s


def render_coach_notes(
    raw_notes: list[dict[str, Any]],
    registry: MoveRegistry,
    decision_map: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Render the Pass-4 LLM's raw move picks ``{move_id, subject, finding_refs}`` into
    deterministic coaching prose from each move's LOCKED template. The subject validator gates
    every note (an invalid/imperative/code-bearing subject ⇒ no coaching for that finding); a
    ``move_id`` NOT in ``registry`` (e.g. a pick outside the applicable subset) is dropped — so
    the LLM can never select a move outside the applicable set.

    ``decision_map`` (story 8086: coaching over blocking findings too) maps finding id →
    ``"block" | "advisory"``; when given, each rendered note is stamped with the decision of
    its referenced finding(s) ("block" wins when a note references both) so consumers can
    distinguish must-fix coaching from optional coaching."""
    notes: list[dict[str, Any]] = []
    for n in raw_notes:
        move = registry.get(n.get("move_id", ""))
        subject = validate_subject(n.get("subject", ""))
        if not move or subject is None:
            continue
        refs = n.get("finding_refs", []) or []
        note = {
            "move_id": n["move_id"],
            "move_name": move["name"],
            "subject": subject,
            "finding_refs": refs,
            "coaching": move["template"].format(subject=subject),
        }
        if decision_map is not None:
            decisions = {decision_map.get(str(r)) for r in refs} - {None}
            if decisions:
                note["decision"] = "block" if "block" in decisions else "advisory"
        notes.append(note)
    return notes


# The injected LLM seam: given the coach INSTRUCTIONS (built over the applicable moves) and the
# applicable registry, return the raw move picks ``[{move_id, subject, finding_refs}]``. The
# workflow shell (the v3 engine's coach prompt step) is one such seam; a FakeRunner-backed
# callable is the offline seam; b744 supplies its own.
PickMoves = Callable[[str, MoveRegistry], list[dict[str, Any]]]


def coach(
    surviving: list[dict[str, Any]],
    registry: MoveRegistry,
    *,
    pick: PickMoves,
    active_triggers: Iterable[str] = (),
    blocking: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Pass-4 over the surviving findings — blocking + advisory (story 8086: blocking
    findings, the ones an agent MUST remediate, get the same move-shaped coaching; blocking
    entries list first). The mechanism, in order:

    1. **gate-on-coachable>0** — 0 findings (neither bucket) ⇒ return ``[]`` with NO ``pick``
       (LLM) call;
    2. **applicability filter** — keep only the moves that apply given ``active_triggers``;
    3. **pick** — the LLM picks among ONLY the applicable moves (``move_id`` + bounded subject);
    4. **render** — deterministic prose from the move template, gated by the subject validator.

    A pick outside the applicable set is dropped at step 4 (render keys on the applicable
    registry), so the LLM can never select a move outside the applicable subset."""
    coachable = list(blocking) + list(surviving)
    if not coachable:
        return []
    applicable = applicable_moves(registry, active_triggers)
    if not applicable:
        return []
    decision_map = {str(f.get("id")): "block" for f in blocking} | {
        str(f.get("id")): "advisory" for f in surviving
    }
    raw = pick(coach_listing(coachable, applicable), applicable) or []
    return render_coach_notes(raw, applicable, decision_map=decision_map)
