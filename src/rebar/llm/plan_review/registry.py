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

The DET floor (P1–P8) is NOT in this file — it is the ``exec=DET`` tier in
:mod:`.det_floor`. This registry owns the LLM tiers (1-TURN / 2-STEP / AGENT). See
``docs/reuse-surface.md`` §3 for the prompt-library contract this builds on.
"""

from __future__ import annotations

import re
from functools import lru_cache
from importlib import resources
from typing import Any

# Code-grounding is the SOLE responsibility of this set: only these AGENT-tier
# criteria grep/read the live codebase (the orchestrator enforces it — no other
# criterion greps). (criteria_v8 + the three-pass CODEBASE_GROUNDED set.)
CODEBASE_GROUNDED = frozenset({"E4", "G1G2", "A1", "G6"})

# AGENT-tier criteria (one tool-using agent loop each; ~85× a single-turn call) —
# gated by proportionate scrutiny. Container criteria G3/G4 read the LIVE ticket
# graph (one child at a time). T-overlays that depend on what the code actually
# does are agent-tier too.
AGENT_TIER = frozenset(
    {"G1G2", "E4", "A1", "G6", "G3", "G4", "T1", "T3", "T5c", "T8", "T10", "T11"}
)

# The canonical v4 §5 registry — the completeness guard's authority. The DET floor
# P1–P8 live in det_floor.py; BROAD is the orchestrator's bounded open-ended pass.
CANONICAL_DET = ("P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8")
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


class RegistryError(Exception):
    """The criteria registry could not be loaded/validated."""


@lru_cache(maxsize=1)
def _routing_index() -> dict[str, Any]:
    """The derived per-criterion routing index (cached)."""
    import json

    raw = (
        resources.files("rebar.llm.plan_review")
        .joinpath(_ROUTING_RESOURCE)
        .read_text(encoding="utf-8")
    )
    return json.loads(raw)


def _descriptor_from_prompt(cid: str) -> dict[str, Any]:
    """Build a criterion descriptor by merging its prompt-library file (the RUBRIC
    body + facet/exec-mode from front-matter, resolved via the prompt machinery with
    `.rebar/prompts/` overrides) with its routing index entry."""
    from rebar.llm import prompts

    repo_root = None
    try:
        from rebar import config as _config

        repo_root = str(_config.repo_root())
    except Exception:  # noqa: BLE001 — no repo ⇒ packaged prompts only
        repo_root = None
    prompt = prompts.get_prompt(f"{_PROMPT_ID_PREFIX}{cid}", repo_root=repo_root)
    routing = _routing_index().get(cid)
    if routing is None:
        raise RegistryError(f"criterion {cid!r} has no entry in {_ROUTING_RESOURCE}")
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


@lru_cache(maxsize=1)
def load_criteria() -> tuple[dict[str, Any], ...]:
    """Load the production criteria from the prompt library (cached).

    For every criterion in the canonical set, resolve its contract-bearing prompt
    file (project override > packaged) and build its descriptor from the front-matter
    + body. Raises :class:`RegistryError` if a criterion's prompt is missing or
    lacks its contract."""
    out = []
    for cid in sorted(CANONICAL_LLM):
        try:
            out.append(_descriptor_from_prompt(cid))
        except RegistryError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RegistryError(
                f"cannot load criterion prompt for {cid!r} from the prompt library: {exc}"
            ) from exc
    return tuple(out)


def by_id() -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in load_criteria()}


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
def check_registry_coverage() -> tuple[bool, list[str]]:
    """The canonical-registry completeness guard: every LLM criterion in the
    canonical v4 §5 registry must have a contract-bearing prompt FILE in the prompt
    library that loads with its `exec` contract. Returns ``(ok, missing_ids)``.
    (G1G2 is a single combined descriptor; BROAD is the orchestrator's bounded pass,
    not a descriptor.)"""
    missing: list[str] = []
    for cid in sorted(CANONICAL_LLM):
        try:
            _descriptor_from_prompt(cid)
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
