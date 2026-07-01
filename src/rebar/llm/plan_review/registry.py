"""Criteria registry + routing for the plan-review gate (child ca03).

The PRODUCTION criteria live in the workflow-engine **prompt library**, NOT in an
inline constant or the experiment ``criteria_v8.json`` (design reference only). Each
criterion's RUBRIC is a contract-bearing prompt file
(``src/rebar/llm/reviewers/plan_review_<id>.md``, ``category:
plan-review-criterion``) resolved through the da27 prompt machinery
(:func:`rebar.llm.prompts.get_prompt` → front-matter contract + ``.rebar/prompts/``
project overrides). Its ROUTING (``exec`` / ``applies_at`` / ``block_threshold`` /
``default_posture`` / ``checklist``) lives in the derived ``criteria_routing.json``
index — the analog of the reviewers' ``index.json``, which likewise separates prompt
TEXT (library) from selection/routing metadata. :func:`load_criteria` MERGES the two
into a descriptor (32: the Layer-2 judgment F/E/G/A, the T1–T12 overlays, COH, ISF).

This registry provides the generic routing the orchestrator needs:

* :func:`load_criteria` — merge each criterion's library prompt + routing entry (cached).
* :func:`applies` — proportionate-scrutiny filter (``applies_at``: levels /
  container-only / suppress-by-type / suppress-when-test-or-mechanical).
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
      "applies_at": {"levels": [..], "container_only": bool,
                     "suppress_types": [..], "suppress_when": [..]},
      "checklist": [{"key": str, "check": str}, ...],
      "default_posture": "advisory"|"blocking", "block_threshold": float
    }

The DET floor (P1–P9) is NOT in this file — it is the ``exec=DET`` tier in
:mod:`.det_floor`. This registry owns the LLM tiers (1-TURN / 2-STEP / AGENT). See
``docs/reuse-surface.md`` §3 for the prompt-library contract this builds on.
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

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
    {"G1G2", "E4", "A1", "G6", "G3", "G4", "T1", "T3", "T5c", "T8", "T10", "T11"}
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
        "ISF",
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

# ── project-supplied criteria overlay (epic 3156, story ef7e) ───────────────────
# A project may add its OWN plan-review criteria + re-tune/disable a built-in via a
# `.rebar/criteria_routing.json` overlay that REUSES the packaged routing schema, keyed
# by gate:  {"plan_review": {"<id>": {…routing…}}, "code_review": {…}, "activate": […]}.
# A NET-NEW project criterion id MUST be `project.<name>`-prefixed; an un-prefixed
# built-in id is a re-tune/disable of that built-in; a `project.`-id that collides with a
# built-in (or a net-new id that is NOT `project.`-prefixed) is REJECTED at load. A project
# criterion RUNS only if listed in `activate` (presence in the file ≠ active). See
# docs/adr/0015-project-supplied-criteria.md + docs/plan-review-gate.md.
_OVERLAY_FILENAME = "criteria_routing.json"
_GATE_KEY = "plan_review"
_PROJECT_PREFIX = "project."

# Repo-keyed caches for the OVERLAY-merged views. The PACKAGED `_routing_index()` stays
# `@lru_cache`d (immutable per binary); the overlay-merged routing/criteria are `@lru_cache`d
# on (repo_root, overlay content-signature) so a long-lived MCP server serving many repos
# never leaks one repo's routing into another (the G6 cache bug), with bounded LRU eviction
# (maxsize) rather than an unbounded dict. `_invalidate_caches` clears them alongside the
# packaged lru_cache. Editing an overlay yields a NEW signature ⇒ a fresh entry (no stale).


class RegistryError(Exception):
    """The criteria registry could not be loaded/validated."""


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


def _resolve_repo_root(repo_root: str | None) -> str | None:
    """Resolve an overlay discovery root: the explicit arg, else the rebar project root
    (``config.repo_root()`` — the same root :func:`get_prompt` resolves ``.rebar/prompts/``
    overrides against). Returns ``None`` only when there is no resolvable root."""
    if repo_root is not None:
        return str(repo_root)
    try:
        from rebar import config as _config

        return str(_config.repo_root())
    except Exception:  # noqa: BLE001 — no repo ⇒ packaged criteria only
        return None


def _overlay_path(repo_root: str | None) -> Path | None:
    if not repo_root:
        return None
    return Path(repo_root) / ".rebar" / _OVERLAY_FILENAME


def _overlay_signature(repo_root: str | None) -> str:
    """A content signature of the overlay file (sha256 of its bytes, or ``""`` when
    absent) — the cache key that makes an EDIT to the overlay invalidate the memo without
    an explicit clear. Prefer content over mtime (mtime granularity is coarse/flaky)."""
    path = _overlay_path(repo_root)
    if path is None:
        return ""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _load_overlay(repo_root: str | None) -> dict[str, Any] | None:
    """Read + parse the project's ``.rebar/criteria_routing.json`` overlay, or ``None``
    when absent. A malformed overlay is a LOCATED :class:`RegistryError` (never a silent
    skip) — the file path is named so the author can fix it."""
    path = _overlay_path(repo_root)
    if path is None or not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryError(f"cannot read criteria overlay {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"criteria overlay {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryError(
            f"criteria overlay {path} must be a JSON object "
            f"{{'plan_review': {{...}}, 'activate': [...]}}; got {type(data).__name__}"
        )
    return data


def _validate_routing_entry(cid: str, entry: Any, *, where: str) -> None:
    """Structural floor-check on ONE routing entry (located error). Mirrors the shape the
    packaged index carries so an overlay entry can't smuggle a malformed record past load."""
    if not isinstance(entry, dict):
        raise RegistryError(
            f"{where}: routing for {cid!r} must be an object, got {type(entry).__name__}"
        )
    exec_v = entry.get("exec", "1-TURN")
    if not isinstance(exec_v, str) or exec_v.upper() not in ("1-TURN", "2-STEP", "AGENT", "DET"):
        raise RegistryError(
            f"{where}: criterion {cid!r} has invalid exec {exec_v!r} "
            "(expected one of 1-TURN / 2-STEP / AGENT / DET)"
        )
    bt = entry.get("block_threshold", 0.95)
    if not isinstance(bt, (int, float)) or isinstance(bt, bool) or not (0.0 <= float(bt) <= 1.0):
        raise RegistryError(
            f"{where}: criterion {cid!r} block_threshold must be a number in [0,1], got {bt!r}"
        )
    posture = entry.get("default_posture", "advisory")
    if posture not in ("advisory", "blocking"):
        raise RegistryError(
            f"{where}: criterion {cid!r} default_posture must be 'advisory' or 'blocking', "
            f"got {posture!r}"
        )
    # fail_mode governs an exec:DET criterion's abstain posture (fail-open records coverage;
    # fail-closed blocks on absence). Validated only when present (a non-DET / silent entry
    # defaults to "open" downstream), mirroring the code-review consumer's default.
    if "fail_mode" in entry and entry.get("fail_mode") not in ("open", "closed"):
        raise RegistryError(
            f"{where}: criterion {cid!r} fail_mode must be 'open' or 'closed', "
            f"got {entry.get('fail_mode')!r}"
        )


def effective_routing(repo_root: str | None = None) -> dict[str, Any]:
    """The packaged routing index MERGED with the project overlay's ``plan_review`` map
    (repo-keyed, memoized by overlay content-signature — NOT lru-cached, so no cross-repo
    leakage). Overlay merge rules (each violation is a LOCATED load-time error):

    * an un-prefixed **built-in** id ⇒ re-tune (routing merged over the packaged entry);
    * a ``project.<name>``-prefixed id ⇒ a net-new project criterion (added);
    * a ``project.``-id equal to a built-in id ⇒ REJECT (a project id can never rebind a
      built-in); a net-new id that is NOT ``project.``-prefixed ⇒ REJECT (must be namespaced)."""
    rr = _resolve_repo_root(repo_root)
    return _effective_routing_cached(rr or "", _overlay_signature(rr))


@lru_cache(maxsize=128)
def _effective_routing_cached(rr: str, _overlay_sig: str) -> dict[str, Any]:
    """The (repo_root, overlay-signature)-keyed compute for :func:`effective_routing`. The
    signature is a pure CACHE KEY (an overlay edit ⇒ a new key ⇒ a fresh compute); the merge
    reads the overlay bytes fresh. ``rr == ""`` means no resolvable repo (packaged-only)."""
    rr_arg: str | None = rr or None
    merged: dict[str, Any] = dict(_routing_index())
    overlay = _load_overlay(rr_arg)
    if overlay is not None:
        gate = overlay.get(_GATE_KEY) or {}
        if not isinstance(gate, dict):
            raise RegistryError(
                f"criteria overlay {_overlay_path(rr_arg)}: '{_GATE_KEY}' must be an object of "
                f"{{id: routing}}, got {type(gate).__name__}"
            )
        where = f"criteria overlay {_overlay_path(rr_arg)} [{_GATE_KEY}]"
        for cid, entry in gate.items():
            _validate_routing_entry(cid, entry, where=where)
            is_builtin = cid in CANONICAL_LLM
            if cid.startswith(_PROJECT_PREFIX):
                if is_builtin:
                    raise RegistryError(
                        f"{where}: project id {cid!r} collides with a built-in criterion "
                        "(a project criterion can never rebind a built-in)"
                    )
                merged[cid] = entry
            elif is_builtin:
                merged[cid] = {**merged[cid], **entry}  # re-tune: overlay wins per-key
            else:
                raise RegistryError(
                    f"{where}: net-new criterion id {cid!r} must be "
                    f"'{_PROJECT_PREFIX}<name>'-prefixed "
                    "(an un-prefixed id may only re-tune an existing built-in)"
                )
    return merged


def effective_criteria(repo_root: str | None = None) -> tuple[str, ...]:
    """The ACTIVE criterion-id vocabulary for a repo = ``CANONICAL_LLM`` ∪ the project ids
    listed in the overlay's ``activate`` list (presence in the file ≠ active). An activated
    project id with no routing entry, or a non-``project.`` id in ``activate``, is a LOCATED
    load-time error. This is THE seam that opens the closed vocabulary — route it through
    every plan-review vocabulary callsite (``load_criteria`` / ``check_registry_coverage`` /
    the workflow Pass-1 batch vocab)."""
    rr = _resolve_repo_root(repo_root)
    overlay = _load_overlay(rr)
    ids = set(CANONICAL_LLM)
    if overlay is not None:
        activate = overlay.get("activate") or []
        if not isinstance(activate, list):
            raise RegistryError(
                f"criteria overlay {_overlay_path(rr)}: 'activate' must be a list of ids, "
                f"got {type(activate).__name__}"
            )
        routing = effective_routing(rr)
        for aid in activate:
            if not isinstance(aid, str):
                raise RegistryError(
                    f"criteria overlay {_overlay_path(rr)}: 'activate' entries must be strings"
                )
            if aid in CANONICAL_LLM:
                continue  # activating a built-in is a no-op (built-ins are always active)
            if not aid.startswith(_PROJECT_PREFIX):
                raise RegistryError(
                    f"criteria overlay {_overlay_path(rr)}: activated id {aid!r} must be a "
                    f"'{_PROJECT_PREFIX}<name>' project criterion (built-ins are always active)"
                )
            if aid not in routing:
                raise RegistryError(
                    f"criteria overlay {_overlay_path(rr)}: activated criterion {aid!r} has no "
                    f"'{_GATE_KEY}' routing entry"
                )
            ids.add(aid)
    return tuple(sorted(ids))


def _detector_matches(detector_id: str, selector: dict[str, Any] | None) -> bool:
    """True iff ``detector_id`` matches a routing ``detector`` selector — an exact ``id`` or an
    ``id_prefix`` class (the same selector grammar the code-review consumer reads)."""
    if not selector:
        return False
    exact = selector.get("id")
    if exact is not None and detector_id == exact:
        return True
    pref = selector.get("id_prefix")
    return pref is not None and detector_id.startswith(pref)


def _det_scenario(routing: dict[str, Any], repo_root: str | None) -> str | None:
    """The human-readable "scenario" for an exec:DET criterion = the message of the first detector
    its ``detector`` selector resolves to (from the on-disk detector registry). Returns ``None``
    when no selector / no matching detector / no message (the caller falls back to name / id).
    Fail-open: any registry-load error yields ``None`` (a DET descriptor never depends on the
    detector suite being installed)."""
    selector = routing.get("detector")
    if not selector:
        return None
    try:
        from rebar.grounding.detectors import load_registry

        reg = load_registry(repo_root)
    except Exception:  # noqa: BLE001 — the detector suite is optional; a missing registry ⇒ fallback
        return None
    for det in reg:
        if _detector_matches(det.id, selector):
            msg = (det.rule or {}).get("message")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()
            return None
    return None


def _descriptor_from_prompt(
    cid: str, *, repo_root: str | None = None, routing_index: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build a criterion descriptor by merging its prompt-library file (the RUBRIC
    body + facet/exec-mode from front-matter, resolved via the prompt machinery with
    `.rebar/prompts/` overrides) with its routing index entry. ``routing_index`` may be a
    pre-resolved :func:`effective_routing` map (avoids re-reading the overlay per criterion)."""
    from rebar.llm import prompts

    rr = _resolve_repo_root(repo_root)
    routing_map = routing_index if routing_index is not None else effective_routing(rr)
    routing = routing_map.get(cid)
    if routing is None:
        raise RegistryError(f"criterion {cid!r} has no entry in {_ROUTING_RESOURCE}")
    # exec:DET criteria are PROMPT-LESS (a pattern-rule detector, not an LLM rubric), so they must
    # NOT resolve a prompt-library file (story 7f0d). Build the descriptor from the routing entry
    # alone — the "scenario" is the detector's rule message (resolved from the detector registry
    # via the routing `detector` selector), never a prompt body. This keeps `load_criteria` from
    # blowing up on an activated project DET criterion that ships no `.rebar/prompts/…` file.
    if str(routing.get("exec", "")).upper() == "DET":
        return {
            "id": cid,
            "exec": "DET",
            "facet": routing.get("facet", cid),
            "name": routing.get("name", cid),
            "scenario": _det_scenario(routing, rr) or routing.get("name") or cid,
            "applies_at": routing.get("applies_at", {}),
            "checklist": [],
            "block_threshold": routing.get("block_threshold", 0.95),
            "default_posture": routing.get("default_posture", "advisory"),
            "fail_mode": routing.get("fail_mode", "open"),
            "detector": routing.get("detector"),
            "routing": routing.get("routing"),
            "trigger": None,
            "overlay_routing": None,
        }
    prompt = prompts.get_prompt(f"{_PROMPT_ID_PREFIX}{cid}", repo_root=rr)
    return {
        "id": cid,
        "exec": routing.get("exec", "1-TURN"),
        "facet": prompt.dimension or routing.get("facet", "misc"),
        "name": prompt.title or cid,
        "scenario": prompt.text.strip(),
        "applies_at": routing.get("applies_at", {}),
        "checklist": routing.get("checklist", []),
        "block_threshold": routing.get("block_threshold", 0.95),
        "default_posture": routing.get("default_posture", "advisory"),
        "routing": routing.get("routing"),
        "trigger": routing.get("trigger"),
        "overlay_routing": routing.get("overlay_routing"),
    }


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


def is_mechanical_leaf(plan: str, ticket_type: str | None) -> bool:
    return ticket_type == "task" and bool(_MECHANICAL_RE.search(plan or ""))


def applies(
    crit: dict[str, Any],
    *,
    level: str,
    has_children: bool = False,
    ticket_type: str | None = None,
    plan: str = "",
) -> bool:
    """Proportionate-scrutiny filter from the criterion's ``applies_at`` field:
    skip leaf-implementation criteria at epic/story altitude, container criteria
    when there are no children, type-suppressed criteria (e.g. bugs), and
    suppress-when conditions (test-task / mechanical-leaf). Defaults are permissive
    (run everywhere) when ``applies_at`` is absent."""
    ap = crit.get("applies_at") or {}
    if ticket_type and ticket_type in (ap.get("suppress_types") or []):
        return False
    levels = ap.get("levels") or ["epic", "story", "task"]
    if level not in levels:
        return False
    if ap.get("container_only") and not has_children:
        return False
    for cond in ap.get("suppress_when") or []:
        if cond == "test_task" and is_test_task(plan):
            return False
        if cond == "mechanical_leaf" and is_mechanical_leaf(plan, ticket_type):
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
    return 0.5 if ticket_size in ("large", "epic", "has_children") else 1.0


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


def _main(argv: list[str] | None = None) -> int:
    """``python -m rebar.llm.plan_review.registry validate-routing`` — the CI parity gate."""
    import sys

    args = sys.argv[1:] if argv is None else argv
    if args[:1] != ["validate-routing"]:
        print("usage: python -m rebar.llm.plan_review.registry validate-routing", file=sys.stderr)  # noqa: T201
        return 2
    problems = validate_packaged_routing()
    if problems:
        print("criteria_routing.json parity gate FAILED:", file=sys.stderr)  # noqa: T201
        for p in problems:
            print(f"  - {p}", file=sys.stderr)  # noqa: T201
        return 1
    print(f"criteria_routing.json parity gate: OK ({len(_routing_index())} criteria in sync).")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
