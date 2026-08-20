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
into a descriptor (e.g. the Layer-2 judgment F/E/G/A, the T1–T15 overlays, COH, ISF,
and the advisory ac-text-quality / scope criteria).

This registry provides the generic routing the orchestrator needs:

* :func:`load_criteria` — merge each criterion's library prompt + routing entry (cached).
* :func:`applies` — proportionate-scrutiny filter (``applies_at``: container/leaf
  ``scope`` / suppress-by-type / suppress-when-test-or-mechanical).
* :func:`chunk_by_facet` — pack same-``facet`` single-turn criteria into
  ``ceil(total/n)`` near-equal balanced chunks (``n = base_chunk(model) ×
  size_factor(ticket)``, floored at 2), never a wasteful singleton for ``total >= 2``
  (the RUBRIC is the lever that fits a context window — the ticket content is NEVER
  chunked).
* :func:`overlay_triggers` / :func:`leaf_gate_triggers` — deterministic criterion
  pre-gates (T5a/T5d/T7/T12 + the ticket-4ee2 T13/T14 and leaf-criterion pre-filters,
  tables in :mod:`.det_gate_rules`); the rest are LLM-routed at Pass-1.
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

The DET floor (P1–P11) is NOT in this file — it is the ``exec=DET`` tier in
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

# The guide-rendering cluster (extracted along a call-graph seam so this module stays under the
# size cap) — re-exported so every historical ``registry.<name>`` reference (guide_parity, the
# MCP read tool, the CLI, the library, the explain tests) resolves unchanged. criteria_guide
# imports THIS module only lazily inside its functions, so this top-level import is never
# circular; ``ExplainError`` / ``explain_guide`` / ``AUTHOR_GUIDES`` stay here (below).
from .criteria_guide import _guide_path as _guide_path
from .criteria_guide import _guide_section_body as _guide_section_body
from .criteria_guide import _guide_sections as _guide_sections
from .criteria_guide import explain_criterion as explain_criterion
from .criteria_guide import regenerate_criteria_guide as regenerate_criteria_guide
from .criteria_guide import validate_criteria_guide as validate_criteria_guide
from .det_gate_rules import _DET_LEAF_GATE_RULES as _DET_LEAF_GATE_RULES
from .det_gate_rules import _DET_OVERLAY_RULES as _DET_OVERLAY_RULES

# The deterministic criterion pre-gates (ticket 4ee2) — re-exported so this registry
# stays the routing seam every call site imports (see the overlay-triggering section).
from .det_gate_rules import DetGateRule as DetGateRule
from .det_gate_rules import leaf_gate_triggers as leaf_gate_triggers
from .det_gate_rules import overlay_triggers as overlay_triggers
from .det_gate_rules import project_trigger_fires as project_trigger_fires

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
CODEBASE_GROUNDED = frozenset({"E4", "G1G2", "A1", "G6", "asserted-capability", "evidence-kind"})

# AGENT-tier criteria (one tool-using agent loop each; ~85× a single-turn call) —
# gated by proportionate scrutiny. Container criteria G3/G4 read the LIVE ticket
# graph (one child at a time). T-overlays that depend on what the code actually
# does are agent-tier too.
AGENT_TIER = frozenset(
    {
        "G1G2",
        "E4",
        "A1",
        "G6",
        "G3",
        "G4",
        "G7",
        "T1",
        "T3",
        "T5c",
        "T8",
        "T10",
        "T11",
        "asserted-capability",
        "evidence-kind",
        "decomp-shape",
    }
)

# The canonical v4 §5 registry — the completeness guard's authority. The DET floor
# P1–P11 live in det_floor.py, which re-exports the checks its size seam moved to
# siblings (P2/P3/P6/P7 in det_advisory.py, P9 in det_lint.py, P10/P11 in
# det_clarity.py); BROAD is the orchestrator's bounded open-ended pass.
CANONICAL_DET = ("P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10", "P11")
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
        # Cheap 1-TURN joint-satisfiability finder (bug creamy-cocksure-elkhound): can the
        # ticket's own acceptance criteria and declared scope ALL hold at once? Feeds Pass-2's
        # existing `internal_conflict` axis. COH owns cross-SECTION contradiction and disclaims
        # within-section; E1 owns criterion<->description mapping; neither asks this.
        "ac-satisfiability",
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
        # Overlay de-risk (story ea28) — an AGENT-tier overlay that fires when a plan relies
        # on a slow/costly codified loop (CI pipeline, environment/infra apply) to validate
        # runtime-only correctness, and asks the plan to prove the risky mechanism out-of-loop
        # (fast local/manual experiment) BEFORE committing it to the slow loop.
        "T15",
        # Removal-side dual of A1 (epic cite-stone-sea / WS11) — Chesterton's Fence: an advisory,
        # code-grounded gate that fires when a plan removes/weakens an observable behavior, a guard,
        # or an intent-marked artifact, and asks for a grounded triggering scenario.
        "removal-rationale",
        # Asserted-capability grounding probe (epic 6982 / R1) — an advisory, code-grounded
        # AGENT-tier dual of E4 for the finer capability-SURFACE mismatch E4's existence check
        # misses: a plan asserts a named module PROVIDES (or LACKS) a capability it relies on,
        # and the code refutes it (the dc58/db7b/5886 miss class). Ships advisory; promotion to
        # blocking is a future dogfood-gated criteria_routing.json change (see the promotion gate
        # in docs/plan-review-gate.md).
        "asserted-capability",
        # ADR-0043 evidence-kind validator (story deeb): inspect every acceptance item and
        # block only grounded mismatches between its exact tag and where completion proof lives.
        "evidence-kind",
        # Decomposition-shape container criterion (epic 6982 / R3) — an advisory, AGENT-tier
        # CONTAINER criterion (facet `container`, like G3/G4; runs on the pass1_container fan-out)
        # that flags two decomposition-SHAPE smells G3/G4 don't target: a layer-cake split
        # (children partitioned by architectural layer instead of vertical slices) and a
        # consumed-artifact-without-ordering-edge (a child consumes a sibling's artifact with no
        # ordering dependency). Advisory is its PERMANENT posture; promotion to blocking is a
        # future change DOUBLE-gated on dogfood effectiveness AND E6 judge order-stability
        # clearing floor (see the promotion gate in docs/plan-review-gate.md).
        "decomp-shape",
        # Necessity / no-op probe (epic 6982 / R4) — an advisory, single-turn (1-TURN) pass-1
        # criterion that flags a plan which does NOT demonstrate the change is needed: current
        # behavior neither reproduced nor motivated (the FixedBench over-action gap — 35-65% of
        # changes acted without establishing necessity). Distinct from R1 asserted-capability
        # (which greps whether a named module already provides the capability) — this probes
        # whether the plan MOTIVATES the change at all. Also the sole criterion of the light BUG
        # REVIEW TIER (see BUG_TIER_CRITERIA + workflow_ops): bugs run the DET floor + necessity
        # instead of a bare exempt-PASS. Ships advisory; promotion to blocking is a future
        # dogfood-gated criteria_routing.json change (see the promotion gate in
        # docs/plan-review-gate.md).
        "necessity",
        # Advisory sanity check for explicit no-file-impact declarations. A plan that
        # requires source/tests/config/docs contradicts `none`; external-only work does not.
        "no-file-impact",
        # AC process-gate redundancy probe (task sombre-corrective-cob) — an advisory, single-turn
        # (1-TURN) `ac-text-quality` criterion (container+leaf) that flags an acceptance criterion
        # whose ENTIRE completion predicate is a GENERIC development-process / tooling gate CI or
        # rebar already enforces mechanically for every ticket (children-closed, tests/CI/lint
        # pass, plan-review passes, merged, commit-trailer) — so the completion verifier can only
        # focus on ACs that meaningfully represent THIS ticket's delivered work. Accepts an AC
        # naming the ticket's specific deliverable even when tests/CI/plan-review are its subject
        # (e.g. "E2E tests written covering feature X", "plan-review criteria updated to rubric Y").
        # Distinct from evidence-kind (WHERE proof lives) / E1 (coverage) / E2 (ambiguity) /
        # ac-satisfiability (joint satisfiability). Ships advisory; promotion to blocking is a
        # future dogfood-gated criteria_routing.json change (see the promotion gate in
        # docs/plan-review-gate.md).
        "ac-process-gate",
        # Cross-cutting
        "COH",
    }
)

# The light BUG REVIEW TIER (epic 6982 / R4). Bugs are exempt from the full plan-review gate
# (workflow_ops.plan_review_precheck short-circuited every bug to a bare exempt-PASS), so a bug
# got no substantive review. The bug tier instead runs the DET floor + this restricted, ADVISORY
# criteria set — never blocking a bug. Kept to the necessity probe (the over-action gap most
# relevant to a bug-fix plan); `necessity` deliberately does NOT `suppress_types:["bug"]` so it
# applies to bugs. workflow_ops.plan_review_assemble_criteria restricts a bug's included LLM
# criteria to this set.
BUG_TIER_CRITERIA = ("necessity",)

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
# by gate:  {"plan_review": {"<id>": {…routing…}}, "code_review": {…},
#            "activate": {"project.<id>": ["plan_review", "code_review"]}}.
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

        # Plan-review sits on the SAME discovery/resolution seam as code review: `rr` is
        # discovery's root (ambient fallback applied) and `root` is what `get_prompt` will
        # resolve `.rebar/prompts/` against (no fallback — None means packaged only). Today
        # `build_descriptor` hands `rr` straight back, so they always agree; the check makes
        # that agreement ENFORCED rather than incidental, so a future change that diverges
        # them fails loudly instead of reporting a present project rubric as "unknown".
        _overlay_core.check_repo_root_agreement(
            rr, root, where=f"plan-review criterion {criterion_id!r}"
        )
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
    rr_arg: str | None = rr
    routing_map = effective_routing(rr_arg)
    out = []
    for cid in effective_criteria(rr_arg):
        try:
            out.append(_descriptor_from_prompt(cid, repo_root=rr_arg, routing_index=routing_map))
        except RegistryError:
            raise
        except Exception as exc:
            raise RegistryError(
                f"cannot load criterion prompt for {cid!r} from the prompt library: {exc}"
            ) from exc
    return tuple(out)


def by_id(repo_root: str | None = None) -> dict[str, dict[str, Any]]:
    result = {c["id"]: c for c in load_criteria(repo_root)}
    # Focused direct-prerequisite review is deliberately not part of the general
    # routing set: it runs only when the relation preload supplies readable pins.
    result["prerequisite-consistency"] = {
        "id": "prerequisite-consistency",
        "exec": "2-STEP",
        "block_threshold": 0.60,
        "default_posture": "blocking",
        "applies_at": {},
        "checklist": [],
    }
    return result


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
    file_impact_scope: str | None = None,
    ticket_type: str | None = None,
    plan: str = "",
) -> bool:
    """Proportionate-scrutiny filter from the criterion's ``applies_at`` field.

    Scrutiny is keyed on **container vs leaf** — a container has children, a leaf
    does not — never on ticket TYPE (epic/story/task): a childless epic is a leaf,
    a story with children is a container. A criterion's ``scope`` lists the nodes it
    runs at (subset of ``["container", "leaf"]``; absent ⇒ both). ``suppress_types``
    (the bug/session_log exemption axis) and the ``suppress_when`` conditions
    (test-task / mechanical-leaf) still apply. ``require_file_impact_scope``
    selects an explicit persisted declaration kind. Defaults are permissive (run
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
    required_file_impact_scopes = ap.get("require_file_impact_scope")
    if required_file_impact_scopes and file_impact_scope not in required_file_impact_scopes:
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
    """Pack facet-ordered criteria into ``ceil(total/n)`` contiguous, near-equal
    balanced chunks (sizes ``total//k`` and ``total//k + 1``, where ``n`` is
    ``base_chunk × size_factor`` clamped to a floor of 2). For ``total >= 2`` no chunk
    is ever a wasteful singleton — a trailing remainder is redistributed, and in the
    ``n == 2`` degenerate (haiku/local + halved) where an odd total cannot split into
    all-size-2 chunks, the forced trailing singleton is merged back into the previous
    chunk (making it size 3). Chunks are within ``[2, n]`` for ``n >= 3``; only that
    ``n == 2`` merge can reach ``n + 1``, and only the degenerate ``total == 1`` yields
    a lone 1-element chunk. Single-turn / 2-step tier only — AGENT criteria run one
    per call (not chunked). The ticket CONTENT is never split; only the rubric."""
    n = max(2, round(base_chunk(model) * size_factor(ticket_size)))
    by_facet: dict[str, list] = {}
    for c in crits:
        by_facet.setdefault(c.get("facet", "misc"), []).append(c)
    ordered = [c for facet in sorted(by_facet) for c in by_facet[facet]]
    total = len(ordered)
    if total == 0:
        return []
    # Balance into ceil(total/n) contiguous chunks of near-equal size (differ by <=1) rather
    # than fixed n-slices, so a trailing remainder never lands in a wasteful singleton chunk
    # (a count total%n == 1 would otherwise strand one criterion in its own LLM call). Facet
    # adjacency is preserved because the split stays contiguous over the facet-ordered list.
    k = (total + n - 1) // n
    base, extra = divmod(total, k)
    chunks: list[list[dict[str, Any]]] = []
    start = 0
    for j in range(k):
        size = base + (1 if j < extra else 0)
        chunks.append(ordered[start : start + size])
        start += size
    # n == 2 with an odd total cannot make every chunk size 2 without exceeding n; fold the
    # unavoidable trailing singleton into its predecessor (size 3) so no lone 1-criterion call
    # is ever emitted for total >= 2.
    if len(chunks) >= 2 and len(chunks[-1]) == 1:
        chunks[-2].extend(chunks.pop())
    return chunks


# ── overlay triggering (deterministic where low-FP; else LLM-routed) ────────────
# The deterministic criterion pre-gates (the DetGateRule schema, the overlay table —
# T5a/T5d/T7/T12 plus the ticket-4ee2 T13/T14 pre-filters — and the parallel LEAF-gate
# table for removal-rationale/asserted-capability) live in det_gate_rules.py: the
# verbatim audit trigger vocabularies would push this module past the 800-LOC cap.
# Re-exported (imported at the top of this module) so this registry stays the routing
# seam every call site imports: ``registry.overlay_triggers`` / ``registry.leaf_gate_triggers``
# / ``registry._DET_OVERLAY_RULES`` / ``registry._DET_LEAF_GATE_RULES`` / ``registry.DetGateRule``.

# T8's finder is deliberately broad once it runs: its job is to discover subtle structural
# gaps in an LLM/agent contract.  Applicability itself therefore cannot safely be delegated to
# that same stochastic call.  Keep this boundary deterministic and limited to language that
# explicitly names an LLM/agent surface.  Generic type-shape words (``schema``, ``enum``,
# ``dataclass``) are intentionally absent: they are common in ordinary deterministic software
# and are covered by the non-overlay criteria.
_T8_LLM_SURFACE_RE = re.compile(
    r"\b(?:"
    r"llms?|large language models?|language models?|generative ai|"
    r"openai|anthropic|claude|chatgpt|gpt(?:[- ]?\d[\w.-]*)?|gemini|mcp tools?|"
    r"(?:system|developer|reviewer|agent|model) prompts?|"
    r"prompt templates?|prompt librar(?:y|ies)|prompt engineering|"
    r"sub[- ]?agents?|agentic|ai agents?|"
    r"agents? (?:system|workflow|runner|reviewer|instruction|behavior|behaviour)|"
    r"tool[- ]calling|function[- ]calling|"
    r"tools? (?:input|output|request|response) schemas?"
    r")\b",
    re.IGNORECASE,
)


def t8_llm_surface_applies(plan: str) -> bool:
    """Return whether ``plan`` explicitly defines an LLM/agent surface for T8.

    T8 is a structural-completeness probe for prompts, model/agent behaviour, and schemas
    consumed or emitted through an LLM/tool structured-output contract.  Ordinary typed data,
    deterministic adapters, and CLI readers do not become T8-applicable merely because they
    mention schemas, enums, or value objects.
    """
    return bool(_T8_LLM_SURFACE_RE.search(plan or ""))


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
# The guide-rendering cluster (``_guide_path`` / ``_guide_section_body`` /
# ``regenerate_criteria_guide`` / ``_guide_sections`` / ``validate_criteria_guide`` /
# ``explain_criterion``) lives in the sibling :mod:`.criteria_guide` module (registry sits at
# the module-size cap) and is RE-EXPORTED at the bottom of this module, so every
# ``registry.<name>`` call site is unchanged. ``ExplainError`` and the author-facing prose
# guides (``explain_guide`` / ``AUTHOR_GUIDES``) stay here — the latter is the
# ``registry.resources`` monkeypatch surface the explain tests pin.


class ExplainError(RegistryError):
    """A criterion-explain lookup failure. ``kind`` is the failing state shared across all three
    surfaces (CLI / MCP / library): ``unknown-id`` / ``malformed-registry`` / ``missing-file``."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# ── Author-facing prose guides (the on-ramp, distinct from the generated criterion registry) ──
# Hand-written, whole-file guides an author reads BEFORE writing a plan / pushing a change. Unlike
# the criterion sections above (derived from the registry), these are prose. They are PACKAGED
# under `rebar._guides` (canonical home; the `docs/` copies are thin pointers) and read via
# `importlib.resources` so `rebar explain plan` / `rebar explain review` and the MCP tool serve
# them verbatim from any installation. Values are the bare resource filenames under that package.
AUTHOR_GUIDES: dict[str, str] = {
    "plan": "writing-a-passing-plan.md",
    "review": "passing-code-review.md",
    "commit-trailer": "commit-ticket-trailer.md",
}

_GUIDE_PACKAGE = "rebar._guides"


def explain_guide(name: str) -> str:
    """Return an author-facing prose guide by short name (see :data:`AUTHOR_GUIDES`), read from the
    packaged ``rebar._guides`` resources — repo-root independent, so an installed rebar serves it
    from any working directory. Shares the :class:`ExplainError` contract with
    :func:`explain_criterion` — ``unknown-id`` for a name not in the map, ``missing-file`` if the
    packaged resource is somehow absent (a broken install)."""
    filename = AUTHOR_GUIDES.get(name)
    if filename is None:
        raise ExplainError(
            "unknown-id", f"unknown guide {name!r}; known: {', '.join(sorted(AUTHOR_GUIDES))}"
        )
    try:
        return (resources.files(_GUIDE_PACKAGE) / filename).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise ExplainError(
            "missing-file", f"packaged author guide {filename!r} is unavailable: {exc}"
        ) from exc


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
    # a removed/renamed guide section fails validate-routing — PLUS the criterion-pin manifest
    # for the hand-written prose guides (bug 828a), so a criterion whose text moves out from
    # under a sentence that cites it can no longer drift silently.
    #
    # Imported LAZILY (the pattern `_guide_path` already uses for `rebar.config`): guide_parity
    # imports CANONICAL_LLM / _guide_section_body / AUTHOR_GUIDES / load_criteria back out of
    # this module, so a top-level import here would be circular. The call resolves the function
    # off the module at call time so a monkeypatch of it is honoured.
    from . import guide_parity

    problems = (
        validate_packaged_routing()
        + validate_criteria_guide()
        + guide_parity.validate_guide_criterion_pins()
    )
    if problems:
        print("criteria_routing.json parity gate FAILED:", file=sys.stderr)  # noqa: T201
        for p in problems:
            print(f"  - {p}", file=sys.stderr)  # noqa: T201
        return 1
    print(f"criteria_routing.json parity gate: OK ({len(_routing_index())} criteria in sync).")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
