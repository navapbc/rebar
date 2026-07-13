"""Criteria registry + routing for the plan-review gate (child ca03).

The PRODUCTION criteria live in the workflow-engine **prompt library**, NOT in an
inline constant or the experiment ``criteria_v8.json`` (design reference only). Each
criterion's RUBRIC is a contract-bearing prompt file
(``src/rebar/llm/reviewers/plan_review_<id>.md``, ``category:
plan-review-criterion``) resolved through the da27 prompt machinery
(:func:`rebar.llm.prompting.prompts.get_prompt` → front-matter contract + ``.rebar/prompts/``
project overrides). Its ROUTING (``exec`` / ``applies_at`` / ``block_threshold`` /
``default_posture`` / ``checklist``) lives in the derived ``criteria_routing.json``
index — the analog of the reviewers' ``index.json``, which likewise separates prompt
TEXT (library) from selection/routing metadata. :func:`load_criteria` MERGES the two
into a descriptor (32: the Layer-2 judgment F/E/G/A, the T1–T12 overlays, COH, ISF).

This registry provides the generic routing the orchestrator needs:

* :func:`load_criteria` — merge each criterion's library prompt + routing entry (cached).
* :func:`applies` — proportionate-scrutiny filter (``applies_at``: container/leaf
  ``scope`` / suppress-by-type / suppress-when-test-or-mechanical).
* :func:`chunk_by_facet` — pack same-``facet`` single-turn criteria into chunks of
  ``base_chunk(model) × size_factor(ticket)`` (the RUBRIC is the lever that fits a
  context window — the ticket content is NEVER chunked).
* :func:`overlay_triggers` — deterministic low-FP overlay triggers (T5a/T5d/T7/T12),
  the rest are LLM-routed at Pass-1.
* :func:`check_registry_coverage` — the completeness guard (every criterion in the
  canonical v4 §5 registry must have a loadable library prompt + routing entry).

The merged descriptor (per criterion)::

    {
      "id": str, "exec": "1-TURN"|"2-STEP"|"AGENT", "facet": str,
      "name": str, "scenario": str (the rubric body, from the library prompt),
      "applies_at": {"scope": ["container"|"leaf", ...] (absent ⇒ both),
                     "suppress_types": [..], "suppress_when": [..]},
      "checklist": [{"key": str, "check": str}, ...],
      "default_posture": "advisory"|"blocking", "block_threshold": float
    }

The DET floor (P1–P9) is NOT in this file — it is the ``exec=DET`` tier in
:mod:`.det_floor`. This registry owns the LLM tiers (1-TURN / 2-STEP / AGENT). See
``docs/reuse-surface.md`` §3 for the prompt-library contract this builds on.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources
from typing import Any

from rebar.llm import criteria as _criteria
from rebar.llm.criteria import overlay as _overlay_core

# The gate error is the SHARED criteria error (story 5065): plan-review re-exports it as
# ``RegistryError`` so every existing ``except RegistryError`` / ``pytest.raises`` keeps
# working while the shared layer is the one that actually raises it during delegation.
RegistryError = _criteria.CriteriaError

# The DESIGNATED code-grounding criteria: the ones whose job is to reason about the
# live codebase (used e.g. to route Pass-2 verification agentic). NOTE: this is NOT a
# tool-capability boundary — agentic tooling (filesystem + rebar) is granted by a
# prompt's ``execution_mode``, not per criterion-id, so EVERY AGENT-tier criterion can
# read code. (Story 2's progressive drift-refresh therefore does NOT reuse a
# "code-blind" subset of findings; it gates whole-verdict reuse on a fresh probe.)
CODEBASE_GROUNDED = frozenset({"E4", "G1G2", "A1", "G6"})

# AGENT-tier criteria (one tool-using agent loop each; ~85× a single-turn call) —
# gated by proportionate scrutiny. Container criteria G3/G4 read the LIVE ticket
# graph (one child at a time). T-overlays that depend on what the code actually
# does are agent-tier too.
AGENT_TIER = frozenset(
    {"G1G2", "E4", "A1", "G6", "G3", "G4", "G7", "T1", "T3", "T5c", "T8", "T10", "T11"}
)

# The canonical v4 §5 registry — the completeness guard's authority. The DET floor
# P1–P9 live in det_floor.py; BROAD is the orchestrator's bounded open-ended pass.
CANONICAL_DET = ("P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9")
CANONICAL_LLM = frozenset(
    {
        # Layer-2 judgment
        "F1",
        "F4",
        "E1",
        "E2",
        "E3",
        "E5",
        "E6",
        "G1G2",
        "G3",
        "G4",
        "E4",
        "A1",
        "G5",
        "G6",
        "G7",
        "ISF",
        # Cheap 1-TURN provenance finder (epic cite-stone-sea / WS2) — hedged-requirement
        # signal feeding Pass-2's committed_work_relies_on_unbacked_claim. See ADR 0033.
        "hedge",
        # Triggered overlays
        "T1",
        "T2",
        "T3",
        "T4",
        "T5a",
        "T5b",
        "T5c",
        "T5d",
        "T5e",
        "T6",
        "T7",
        "T8",
        "T9",
        "T10",
        "T11",
        "T12",
        # LLM-routed enumeration overlays (epic cite-stone-sea / WS3, ADR 0034). Gap-report
        # G-5/G-10 → Txx ids (G5 is taken; is_overlay needs the Txx pattern). Each enumerates an
        # INVISIBLE affected set in its own agentic context window: T13 prohibition→call-sites,
        # T14 new-ref/event→workflow-trigger filters + release-infra.
        "T13",
        "T14",
        # Removal-side dual of A1 (epic cite-stone-sea / WS11) — Chesterton's Fence: an advisory,
        # code-grounded gate that fires when a plan removes/weakens an observable behavior, a guard,
        # or an intent-marked artifact, and asks for a grounded triggering scenario.
        "removal-rationale",
        # Cross-cutting
        "COH",
    }
)

# Each criterion's RUBRIC is a contract-bearing PROMPT FILE in the workflow-engine
# prompt library (src/rebar/llm/reviewers/plan_review_<id>.md), loaded via the da27
# prompt machinery (get_prompt → front-matter contract + `.rebar/prompts/<id>.md`
# project override). Its ROUTING (exec / applies_at / block_threshold /
# default_posture / checklist) lives in the DERIVED routing index
# (criteria_routing.json) — the analog of the reviewers' index.json, which likewise
# separates prompt TEXT (library) from selection/routing metadata. The production
# criteria do NOT live in the experiment criteria_v8.json (design reference only).
_PROMPT_ID_PREFIX = "plan-review-"
_ROUTING_RESOURCE = "criteria_routing.json"

# ── project-supplied criteria overlay (epic 3156, story ef7e; unified in 5065) ──────
# A project may add its OWN plan-review criteria + re-tune/disable a built-in via a
# `.rebar/criteria_routing.json` overlay that REUSES the packaged routing schema, keyed
# by gate:  {"plan_review": {"<id>": {…routing…}}, "code_review": {…}, "activate": […]}.
# The overlay MERGE / activation / cache-isolation machinery lives in the SHARED
# `rebar.llm.criteria` layer (story 5065); this registry registers the plan-review gate
# with it (its packaged index + canonical set) and its public `effective_*` /
# `disabled_builtins` functions DELEGATE there with `gate_key="plan_review"`. Behaviour is
# byte-identical to ef7e. See docs/adr/0015 + 0017 + docs/plan-review-gate.md.
_GATE_KEY = "plan_review"
# The project-criterion id namespace (a net-new project criterion is `project.<name>`). The
# overlay MERGE uses the shared core's copy; this stays exported for `production_batch_runner`
# (which splits the project subset off `route_criteria`).
_PROJECT_PREFIX = "project."


@lru_cache(maxsize=1)
def _routing_index() -> dict[str, Any]:
    """The PACKAGED per-criterion routing index (immutable per binary, cached). This is
    the built-in routing ONLY — the project overlay is merged by :func:`effective_routing`
    (repo-keyed, uncached) so this cache can never leak a project's routing cross-repo."""
    raw = (
        resources.files("rebar.llm.plan_review")
        .joinpath(_ROUTING_RESOURCE)
        .read_text(encoding="utf-8")
    )
    return json.loads(raw)


# Register the plan-review gate with the shared overlay core. `canonical` is read via a
# callable (not a snapshot) so a test that monkeypatches `CANONICAL_LLM` is still honoured
# on a fresh overlay signature — mirroring how ef7e read the module global inside the cache.
_criteria.register_gate(
    _GATE_KEY,
    packaged_index=_routing_index,
    canonical=lambda: CANONICAL_LLM,
)


# The overlay discovery + signature helpers are the SHARED core's (story 5065); these thin
# aliases keep the internal callsites (`load_criteria`, `_descriptor_from_prompt`,
# `check_registry_coverage`) unchanged. `_validate_routing_entry` is re-exported for the
# packaged-routing parity gate below.
_resolve_repo_root = _overlay_core._resolve_repo_root
_overlay_signature = _overlay_core._overlay_signature
_validate_routing_entry = _overlay_core._validate_routing_entry


def effective_routing(repo_root: str | None = None) -> dict[str, Any]:
    """The packaged routing index MERGED with the project overlay's ``plan_review`` map
    (repo-keyed, memoized by overlay content-signature — so no cross-repo leakage). DELEGATES
    to the shared :func:`rebar.llm.criteria.effective_routing` with ``gate_key="plan_review"``;
    the merge rules (re-tune / net-new namespace / collision reject) are unchanged (story 5065).

    * an un-prefixed **built-in** id ⇒ re-tune (routing merged over the packaged entry);
    * a ``project.<name>``-prefixed id ⇒ a net-new project criterion (added);
    * a ``project.``-id equal to a built-in id ⇒ REJECT (a project id can never rebind a
      built-in); a net-new id that is NOT ``project.``-prefixed ⇒ REJECT (must be namespaced)."""
    return _criteria.effective_routing(repo_root, gate_key=_GATE_KEY)


def effective_criteria(repo_root: str | None = None) -> tuple[str, ...]:
    """The ACTIVE criterion-id vocabulary for a repo = ``CANONICAL_LLM`` ∪ the project ids
    listed in the overlay's ``activate`` list (presence in the file ≠ active), minus any
    disabled built-in. DELEGATES to the shared :func:`rebar.llm.criteria.effective_criteria`
    with ``gate_key="plan_review"``. This is THE seam that opens the closed vocabulary — route
    it through every plan-review vocabulary callsite (``load_criteria`` /
    ``check_registry_coverage`` / the workflow Pass-1 batch vocab)."""
    return _criteria.effective_criteria(repo_root, gate_key=_GATE_KEY)


def disabled_builtins(repo_root: str | None = None) -> list[str]:
    """The sorted built-in criterion ids the project overlay DISABLES (a ``"disabled": true``
    key on an un-prefixed built-in routing entry). DELEGATES to the shared
    :func:`rebar.llm.criteria.disabled_builtins` with ``gate_key="plan_review"``. Empty
    (``[]``) when there is no overlay / nothing disabled — so an overlay-absent repo is
    byte-identical to the packaged registry. Story 08af."""
    return _criteria.disabled_builtins(repo_root, gate_key=_GATE_KEY)


def _descriptor_from_prompt(
    cid: str, *, repo_root: str | None = None, routing_index: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build a criterion descriptor by merging its prompt-library file (the RUBRIC body +
    facet/exec-mode from front-matter, resolved via the prompt machinery with `.rebar/prompts/`
    overrides) with its routing index entry. ``routing_index`` may be a pre-resolved
    :func:`effective_routing` map (avoids re-reading the overlay per criterion).

    DELEGATES the exec-tier-polymorphic build to the shared
    :func:`rebar.llm.criteria.build_descriptor` (story 5065): an ``exec:DET`` criterion builds
    a PROMPT-LESS descriptor (story 7f0d's branch); every other tier resolves its rubric via
    the plan-review ``get_prompt`` wrapper passed as ``prompt_getter``."""
    rr = _resolve_repo_root(repo_root)
    routing_map = routing_index if routing_index is not None else effective_routing(rr)
    routing = routing_map.get(cid)
    if routing is None:
        raise RegistryError(f"criterion {cid!r} has no entry in {_ROUTING_RESOURCE}")

    def _get_prompt(criterion_id: str, root: str | None) -> Any:
        from rebar.llm.criteria.ids import criterion_prompt_id
        from rebar.llm.prompting import prompts

        # Decouple the logical criterion id from the rubric's filesystem-safe prompt id
        # (task stew-kid-motif): a project.<name> id reads plan-review-project-<name>.md, so a
        # net-new project criterion — whose dotted id `_valid_id` forbids as a filename — is
        # authorable + loadable. A built-in id maps to plan-review-<id> unchanged.
        return prompts.get_prompt(criterion_prompt_id(criterion_id), repo_root=root)

    return _criteria.build_descriptor(cid, routing, repo_root=rr, prompt_getter=_get_prompt)


def load_criteria(repo_root: str | None = None) -> tuple[dict[str, Any], ...]:
    """Load the ACTIVE criteria (built-ins ∪ activated project criteria) from the prompt
    library, repo-keyed + memoized by overlay content-signature (so no cross-repo leakage).

    For every criterion in :func:`effective_criteria`, resolve its contract-bearing prompt
    file (project override > packaged) and build its descriptor. Raises :class:`RegistryError`
    if a criterion's prompt is missing or lacks its contract."""
    rr = _resolve_repo_root(repo_root)
    return _load_criteria_cached(rr or "", _overlay_signature(rr))


@lru_cache(maxsize=128)
def _load_criteria_cached(rr: str, _overlay_sig: str) -> tuple[dict[str, Any], ...]:
    """The (repo_root, overlay-signature)-keyed compute for :func:`load_criteria` (bounded
    LRU, cross-repo-isolated). ``rr == ""`` means no resolvable repo (packaged-only)."""
    rr_arg: str | None = rr or None
    routing_map = effective_routing(rr_arg)
    out = []
    for cid in effective_criteria(rr_arg):
        try:
            out.append(_descriptor_from_prompt(cid, repo_root=rr_arg, routing_index=routing_map))
        except RegistryError:
            raise
        except Exception as exc:  # noqa: BLE001 — translate ANY prompt-load failure into a RegistryError (re-raised, never swallowed)
            raise RegistryError(
                f"cannot load criterion prompt for {cid!r} from the prompt library: {exc}"
            ) from exc
    return tuple(out)


def by_id(repo_root: str | None = None) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in load_criteria(repo_root)}


# ── proportionate scrutiny (applies_at) ────────────────────────────────────────
_TEST_TASK_RE = re.compile(
    r"\b(red|green|failing test|test[- ]?task|testing mode)\b", re.IGNORECASE
)
_MECHANICAL_RE = re.compile(
    r"\b(refactor|rename|move |extract |dep[- ]?bump|bump (the )?dep|typo|lint|format|"
    r"reformat|inline|delete dead code)\b",
    re.IGNORECASE,
)


def is_test_task(plan: str) -> bool:
    p = plan or ""
    return bool(_TEST_TASK_RE.search(p)) and len(p) < 1400


def is_mechanical_leaf(plan: str, *, has_children: bool = False) -> bool:
    """A mechanical change (refactor/rename/dep-bump/…) at a LEAF (no children).
    Keyed on container/leaf, never on ticket type — a childless ticket of any type
    is a leaf."""
    return not has_children and bool(_MECHANICAL_RE.search(plan or ""))


def applies(
    crit: dict[str, Any],
    *,
    has_children: bool = False,
    has_parent: bool = False,
    ticket_type: str | None = None,
    plan: str = "",
) -> bool:
    """Proportionate-scrutiny filter from the criterion's ``applies_at`` field.

    Scrutiny is keyed on **container vs leaf** — a container has children, a leaf
    does not — never on ticket TYPE (epic/story/task): a childless epic is a leaf,
    a story with children is a container. A criterion's ``scope`` lists the nodes it
    runs at (subset of ``["container", "leaf"]``; absent ⇒ both). ``suppress_types``
    (the bug/session_log exemption axis) and the ``suppress_when`` conditions
    (test-task / mechanical-leaf) still apply. Defaults are permissive (run
    everywhere) when ``applies_at`` is absent."""
    ap = crit.get("applies_at") or {}
    if ticket_type and ticket_type in (ap.get("suppress_types") or []):
        return False
    scope = ap.get("scope") or ["container", "leaf"]
    node = "container" if has_children else "leaf"
    if node not in scope:
        return False
    # `require_parent_id` (G7): a criterion that only runs on a ticket WITH a parent
    # (e.g. leaf-parent-containment). Absent/false ⇒ no parent requirement.
    if ap.get("require_parent_id") and not has_parent:
        return False
    for cond in ap.get("suppress_when") or []:
        if cond == "test_task" and is_test_task(plan):
            return False
        if cond == "mechanical_leaf" and is_mechanical_leaf(plan, has_children=has_children):
            return False
    return True


# ── facet chunking (RUBRIC side only — content is never chunked) ────────────────
def base_chunk(model: str) -> int:
    m = (model or "").lower()
    if "opus" in m:
        return 12
    if "sonnet" in m:
        return 6
    return 3  # haiku / local


def size_factor(ticket_size: str) -> float:
    return 0.5 if ticket_size in ("large", "has_children") else 1.0


def chunk_by_facet(
    crits: list[dict[str, Any]], *, model: str = "claude-sonnet-4-6", ticket_size: str = "moderate"
) -> list[list[dict[str, Any]]]:
    """Pack same-``facet`` criteria into chunks of ``base_chunk × size_factor``
    (clamped to [2, n]). Single-turn / 2-step tier only — AGENT criteria run one
    per call (not chunked). The ticket CONTENT is never split; only the rubric."""
    n = max(2, round(base_chunk(model) * size_factor(ticket_size)))
    by_facet: dict[str, list] = {}
    for c in crits:
        by_facet.setdefault(c.get("facet", "misc"), []).append(c)
    ordered = [c for facet in sorted(by_facet) for c in by_facet[facet]]
    return [ordered[i : i + n] for i in range(0, len(ordered), n)] or []


# ── overlay triggering (deterministic where low-FP; else LLM-routed) ────────────
# Deterministic, low-false-positive triggers only (validated round 4). The rest of
# the overlays (T6/T5b/T9 + the agent-tier T1/T3/T5c/T8/T10/T11) are LLM-routed at
# Pass-1 (a keyword trigger is high-FP for them), so they are NOT listed here.
_DET_OVERLAY_RULES = {
    "T5a": r"\b(latency|throughput|performance|scal\w*|n\+1|batch|cache|memory|hot[- ]?path)\b",
    "T5d": r"\b(ui|button|form|screen|page|modal|wcag|aria|accessib\w*|keyboard|contrast)\b",
    "T7": r"\b(\bdocs?\b|readme|claude\.md|adr|guide|documentation)\b",
    "T12": r"\b(deploy|rollout|canary|feature flag|production traffic|rollback|blue.green)\b",
}


def overlay_triggers(plan: str) -> dict[str, bool]:
    """Deterministic overlay triggers (low-FP set only). Returns ``{overlay_id:
    fired}``. The remaining overlays are LLM-routed and absent from this map."""
    p = plan or ""
    return {ov: bool(re.search(rx, p, re.IGNORECASE)) for ov, rx in _DET_OVERLAY_RULES.items()}


# Overlay criterion ids (everything Txx). The orchestrator runs an overlay when it
# is either deterministically triggered (above) OR LLM-routed (the finder decides).
def is_overlay(crit_id: str) -> bool:
    return bool(re.fullmatch(r"T\d+[a-e]?", crit_id))


# ── completeness guard ─────────────────────────────────────────────────────────
def check_registry_coverage(repo_root: str | None = None) -> tuple[bool, list[str]]:
    """The completeness guard: every criterion in the EFFECTIVE set (canonical v4 §5
    built-ins ∪ activated project criteria) must have a contract-bearing prompt FILE in
    the prompt library that loads with its `exec` contract. Returns ``(ok, missing_ids)``.
    ``repo_root=None`` preserves the packaged-only guard (overlay honored when a repo has
    one). (G1G2 is a single combined descriptor; BROAD is the orchestrator's bounded pass,
    not a descriptor.)"""
    rr = _resolve_repo_root(repo_root)
    routing_map = effective_routing(rr)
    missing: list[str] = []
    for cid in effective_criteria(rr):
        try:
            _descriptor_from_prompt(cid, repo_root=rr, routing_index=routing_map)
        except Exception:  # noqa: BLE001 — missing/malformed prompt ⇒ not covered
            missing.append(cid)
    return (not missing, missing)


def exec_tier(crit: dict[str, Any]) -> str:
    """Normalized exec tier: ``DET`` is owned by det_floor; here we return one of
    ``AGENT`` / ``2-STEP`` / ``1-TURN``."""
    if crit.get("id") in AGENT_TIER or str(crit.get("exec", "")).upper() == "AGENT":
        return "AGENT"
    e = str(crit.get("exec", "1-TURN")).upper()
    return "2-STEP" if e == "2-STEP" else "1-TURN"


# ── packaged-routing parity/validation gate (CI drift gate, epic 3156) ───────────
def validate_packaged_routing() -> list[str]:
    """Validate the PACKAGED ``criteria_routing.json`` and return a list of problems (empty =
    OK). Because the routing is hand-authored judgement (thresholds / applies_at) with no
    derivation source, this is a PARITY gate — not a regenerate-and-diff — mirroring the
    ``reviewers/index.json`` drift gate in spirit: it fails CI when the committed routing
    drifts out of sync with the canonical vocabulary or carries a malformed entry.

    Checks: (1) every ``CANONICAL_LLM`` criterion has a routing entry; (2) no ORPHAN routing
    entry (a key not in ``CANONICAL_LLM``); (3) every entry passes the structural floor-check
    (``exec`` / ``block_threshold`` / ``default_posture``)."""
    problems: list[str] = []
    routing = _routing_index()
    keys = set(routing)
    for cid in sorted(CANONICAL_LLM - keys):
        problems.append(f"canonical criterion {cid!r} has NO routing entry in {_ROUTING_RESOURCE}")
    for cid in sorted(keys - CANONICAL_LLM):
        problems.append(f"routing entry {cid!r} is an ORPHAN (not in CANONICAL_LLM)")
    for cid in sorted(keys):
        try:
            _validate_routing_entry(cid, routing[cid], where=f"packaged {_ROUTING_RESOURCE}")
        except RegistryError as exc:
            problems.append(str(exc))
    return problems


# ── Criteria authoring guide (R-5, epic cite-stone-sea / WS10) ───────────────────
# A GENERATED, section-keyed Markdown guide (docs/plan-review-criteria-guide.md): one `## <id>`
# section per criterion, DERIVED from the registry (the rubric a plan author must satisfy).
# Regenerated in place (`... registry regenerate-criteria-guide`) and kept honest by
# validate_criteria_guide (folded into the validate-routing gate) — the same regenerate-in-place +
# parity-diff contract as reviewers/index.json. `explain_criterion` is the ONE shared lookup that
# `rebar explain`, the MCP read tool, and the library all wrap.
_GUIDE_RELPATH = ("docs", "plan-review-criteria-guide.md")


class ExplainError(RegistryError):
    """A criterion-explain lookup failure. ``kind`` is the failing state shared across all three
    surfaces (CLI / MCP / library): ``unknown-id`` / ``malformed-registry`` / ``missing-file``."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _guide_path(repo_root_path: str | None = None):  # -> Path
    from rebar import config

    return config.repo_root(repo_root_path).joinpath(*_GUIDE_RELPATH)


def _guide_section_body(criterion: dict[str, Any]) -> str:
    posture = criterion.get("default_posture", "advisory")
    header = f"**{criterion.get('name', '')}** — exec:{criterion.get('exec', '1-TURN')}, {posture}"
    facet = criterion.get("facet", "")
    if facet:
        header += f", facet:{facet}"
    lines = [f"## {criterion['id']}", header, "", (criterion.get("scenario") or "").strip()]
    checklist = criterion.get("checklist") or []
    if checklist:
        lines += ["", "Checklist:"]
        lines += [f"- {c.get('check', c) if isinstance(c, dict) else c}" for c in checklist]
    return "\n".join(lines).rstrip()


def regenerate_criteria_guide(repo_root_path: str | None = None) -> str:
    """Generate docs/plan-review-criteria-guide.md from the registry — one `## <id>` section per
    criterion, sorted by id. Returns the written path (regenerate-in-place; diff detects drift)."""
    criteria = sorted(load_criteria(repo_root=repo_root_path), key=lambda c: c["id"])
    header = (
        "# Plan-review criteria authoring guide\n\n"
        "GENERATED from the criteria registry (`python -m rebar.llm.plan_review.registry "
        "regenerate-criteria-guide`) — do not hand-edit. One `## <criterion-id>` section per "
        "criterion; `rebar explain <criterion-id>` prints a section, and coach deep-links anchor "
        "to `#<criterion-id lower-cased>` (the heading slug).\n"
    )
    body = "\n\n".join(_guide_section_body(c) for c in criteria)
    path = _guide_path(repo_root_path)
    path.write_text(header + "\n" + body + "\n", encoding="utf-8")
    return str(path)


def _guide_sections(text: str) -> dict[str, str]:
    """Parse a guide into ``{criterion-id: section-text}`` keyed by ``## <id>`` headings."""
    out: dict[str, str] = {}
    cur_id: str | None = None
    buf: list[str] = []
    for line in text.split("\n"):
        m = re.match(r"^## (\S+)\s*$", line)
        if m:
            if cur_id is not None:
                out[cur_id] = "\n".join(buf).strip()
            cur_id, buf = m.group(1), [line]
        elif cur_id is not None:
            buf.append(line)
    if cur_id is not None:
        out[cur_id] = "\n".join(buf).strip()
    return out


def validate_criteria_guide(repo_root_path: str | None = None) -> list[str]:
    """Parity: every ``CANONICAL_LLM`` criterion has a ``## <id>`` guide section and the guide
    has no ORPHAN section. Returns problems (empty == in sync). Folded into the routing gate so a
    removed/renamed section fails ``validate-routing``."""
    path = _guide_path(repo_root_path)
    if not path.exists():
        return [f"criteria guide missing at {path} (run regenerate-criteria-guide)"]
    sections = set(_guide_sections(path.read_text(encoding="utf-8")))
    problems = [
        f"criterion {cid!r} has no `## {cid}` section in the criteria guide"
        for cid in sorted(CANONICAL_LLM - sections)
    ]
    problems += [
        f"criteria guide has an ORPHAN section `## {cid}` (not in CANONICAL_LLM)"
        for cid in sorted(sections - CANONICAL_LLM)
    ]
    return problems


def explain_criterion(criterion_id: str, *, repo_root_path: str | None = None) -> str:
    """The ONE shared lookup behind ``rebar explain``, the MCP ``explain_criterion`` tool, and the
    library — returns a criterion's authoring-guide section. Raises :class:`ExplainError` with a
    ``kind`` of ``malformed-registry`` / ``unknown-id`` / ``missing-file``."""
    try:
        ids = {c["id"] for c in load_criteria(repo_root=repo_root_path)}
    except Exception as exc:  # noqa: BLE001 — any registry-load failure is the malformed-registry state
        raise ExplainError("malformed-registry", f"criteria registry is malformed: {exc}") from exc
    if criterion_id not in ids:
        raise ExplainError(
            "unknown-id", f"unknown criterion {criterion_id!r}; known: {', '.join(sorted(ids))}"
        )
    path = _guide_path(repo_root_path)
    if not path.exists():
        raise ExplainError(
            "missing-file",
            f"criteria guide not found at {path}; run "
            "`python -m rebar.llm.plan_review.registry regenerate-criteria-guide`",
        )
    section = _guide_sections(path.read_text(encoding="utf-8")).get(criterion_id)
    if not section:
        raise ExplainError("missing-file", f"criteria guide has no section for {criterion_id!r}")
    return section


def _main(argv: list[str] | None = None) -> int:
    """``python -m rebar.llm.plan_review.registry validate-routing`` — the CI parity gate."""
    import sys

    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else ""
    if cmd == "regenerate-criteria-guide":
        print(f"wrote {regenerate_criteria_guide()}")  # noqa: T201
        return 0
    if cmd != "validate-routing":
        print(  # noqa: T201
            "usage: python -m rebar.llm.plan_review.registry "
            "validate-routing | regenerate-criteria-guide",
            file=sys.stderr,
        )
        return 2
    # The parity gate now covers BOTH the routing index AND the derived criteria guide (WS10) —
    # a removed/renamed guide section fails validate-routing.
    problems = validate_packaged_routing() + validate_criteria_guide()
    if problems:
        print("criteria_routing.json parity gate FAILED:", file=sys.stderr)  # noqa: T201
        for p in problems:
            print(f"  - {p}", file=sys.stderr)  # noqa: T201
        return 1
    print(f"criteria_routing.json parity gate: OK ({len(_routing_index())} criteria in sync).")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
