"""Code-review criteria registry — WS1 seed: the closed overlay-id enum + the
deterministic ``recommend_overlays`` filter + the base-reviewer failure fallback.

This module is the single source of truth for the overlay-id vocabulary. WS1 OWNS
the closed :data:`OVERLAY_IDS` enum (the ids the base reviewer may escalate to);
WS2 ADDS the catalog CONTENT (overlay finder prompts + ``applies_to`` globs + the
``threshold_for`` posture resolver) keyed by these same ids — it never introduces a
new id, so the base reviewer's enum and the catalog can never drift.

The enum is enforced POST-HOC (after the agent returns) by :func:`filter_recommend_overlays`
rather than as a strict JSON-Schema ``enum`` on ``overlay_id``: a ``mode="structured"``
step validates its output against the schema, and a strict enum would turn an
out-of-catalog id into a hard validation error (failing the whole base step). The
contract instead is "the model cannot ESCALATE to an unknown overlay" — an unknown id
is silently DROPPED, never errored — so a hallucinated id costs nothing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import lru_cache
from importlib import resources
from typing import Any, TypeGuard

from rebar.llm import criteria as _criteria

# The code-review gate key in the shared `.rebar/criteria_routing.json` overlay (story 5065).
_GATE_KEY = "code_review"

# ── The closed overlay-id vocabulary (WS1 OWNS this) ──────────────────────────────────────
# The 12 specialist overlays the base reviewer may escalate to. WS2 authors the per-id
# finder prompt + applies_to globs; adding a NEW overlay means adding its id HERE and its
# content in WS2 — the two cannot drift because both derive from this tuple. Most overlays are
# GLOB-triggered (their applies_to globs match the changed files); `deletion-impact` is instead
# CONTENT-triggered (see :func:`content_triggered_overlays`) — it fires on the diff's removed
# def/class/signature lines, so it ships with an empty `applies_to`.
OVERLAY_IDS: tuple[str, ...] = (
    "security",  # authn/authz, secrets, injection, unsafe deserialization
    "performance",  # hot paths, N+1, allocation, complexity regressions
    "i18n",  # localization / encoding / locale-sensitive formatting
    "a11y",  # accessibility (UI/markup/ARIA)
    "db-migrations",  # schema/data migrations, backfills, expand-contract
    "docs",  # user/operator/API docs that must track the change
    "supply-chain",  # dependency / lockfile / vendoring / provenance changes
    "api-compat",  # public API / wire / CLI / config backward-compatibility
    "iac",  # infrastructure-as-code (Terraform/CDK/K8s/Helm/Ansible)
    "tests",  # test sufficiency / regression coverage for the change
    "llm-prompts",  # prompt/contract/output-schema changes to LLM surfaces
    "deletion-impact",  # (content-triggered) removed def/class/signature → dangling references
    "concurrency",  # (content-triggered) threads/async/multiprocessing/locks/channels — races
    "scope-intent",  # (content-triggered) diff vs the UNION scope/AC of the commit's tickets
    "surface-parity",  # (glob-triggered) a write-op's param/guard/required-field surface changed
    # on one adapter (lib/CLI/MCP) without the siblings updated in lockstep
)

# Operational policy (config, not a magic constant baked into the wire schema): the
# escalation reason is a one-liner. An overlong reason is TRUNCATED here, never errored.
REASON_MAX_CHARS = 200

# The dimension tag carried by a coverage-gap note (surfaced in the verdict's `coverage`,
# never run through Pass-2 verify — a coverage gap is not a verifiable claim about the diff).
COVERAGE_GAP_DIMENSION = "coverage-gap"

BASE_REVIEWER_ID = "code-review-base"


def is_overlay_id(value: object) -> TypeGuard[str]:
    """True iff ``value`` is a member of the closed :data:`OVERLAY_IDS` enum (narrows to
    ``str`` for the caller)."""
    return isinstance(value, str) and value in OVERLAY_IDS


def overlay_id_enum() -> list[str]:
    """The overlay-id vocabulary as a list (the single source the base-reviewer prompt
    enumerates and the schema doc references — derived from :data:`OVERLAY_IDS` so a
    drift between the prompt, the schema, and the filter is impossible)."""
    return list(OVERLAY_IDS)


def filter_recommend_overlays(
    raw: Any, *, reason_max: int = REASON_MAX_CHARS
) -> list[dict[str, str]]:
    """Normalize a base reviewer's ``recommend_overlays`` to the valid, bounded set.

    Drops (does NOT error on) any entry whose ``overlay_id`` is not in :data:`OVERLAY_IDS`
    or whose ``reason`` is missing/blank; truncates an overlong ``reason`` to
    ``reason_max``; de-duplicates by ``overlay_id`` (first occurrence wins, preserving
    order). A non-list / malformed input yields ``[]`` (fail-soft — the base reviewer is
    recall-side, never the verdict)."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        oid = entry.get("overlay_id")
        if not is_overlay_id(oid) or oid in seen:
            continue
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            continue
        seen.add(oid)  # oid narrowed to str by is_overlay_id (TypeGuard)
        out.append({"overlay_id": oid, "reason": reason.strip()[:reason_max]})
    return out


def recommend_overlay_ids(raw: Any) -> list[str]:
    """Just the valid, de-duplicated overlay ids from a ``recommend_overlays`` list
    (the escalation signal :mod:`overlay_union` (WS3) unions with the glob triggers)."""
    return [o["overlay_id"] for o in filter_recommend_overlays(raw)]


def coverage_gap_note(detail: str, *, reviewer_id: str = BASE_REVIEWER_ID) -> dict[str, Any]:
    """A finding-shaped coverage-gap note (advisory, low severity). Carried OUTSIDE the
    ``findings`` list (in ``coverage_gaps``) so it surfaces in the verdict's coverage
    without being run through Pass-2 verify."""
    return {
        "dimension": COVERAGE_GAP_DIMENSION,
        "severity": "low",
        "detail": detail,
        "reviewer_id": reviewer_id,
    }


def base_failure_result(reason: str) -> dict[str, Any]:
    """The deterministic fallback when the base reviewer step errors / times out / returns
    no structured output: EMPTY base findings, NO base-driven escalations (Round-B
    membership falls back to glob-triggered overlays only), and a coverage-gap note. Never
    a synthetic BLOCK — the base reviewer is recall-side; deterministic Pass-2/3 still runs
    on whatever findings exist."""
    return {
        "findings": [],
        "recommend_overlays": [],
        "coverage_gaps": [coverage_gap_note(f"base code-reviewer unavailable: {reason}")],
    }


# ── Criteria routing (WS2) — per-criterion posture, read from the committed routing index ──
# Mirrors plan_review/registry.py: the ROUTING (exec / applies_to / default_posture /
# block_threshold / blocking_enabled) lives in the COMMITTED criteria_routing.json (the analog
# of plan-review's), read here by threshold_for. The gate YAML holds NO threshold values.
_ROUTING_RESOURCE = "criteria_routing.json"

# The kernel's default when a criterion has no routing entry (kept in sync with
# review_kernel.DEFAULT_BLOCK_THRESHOLD = 0.95 — the high-threshold, mostly-advisory v1 stance).
DEFAULT_BLOCK_THRESHOLD = 0.95


@lru_cache(maxsize=1)
def routing_index() -> dict[str, Any]:
    """The committed per-criterion routing index (cached). A flat map
    ``{criterion_id: {exec, applies_to, default_posture, block_threshold, blocking_enabled}}``."""
    import json

    raw = resources.files("rebar.llm.code_review").joinpath(_ROUTING_RESOURCE).read_text("utf-8")
    data = json.loads(raw)
    return {k: v for k, v in data.items() if not k.startswith("_")}


# Register the code-review gate with the SHARED overlay core (story 5065) so a project's
# `.rebar/criteria_routing.json` may carry a `code_review` map (net-new `project.` criteria +
# re-tunes) the SAME way plan-review's overlay works. The canonical built-in set is the packaged
# routing's key set (the 11 overlays + the two detector criteria); read via a callable so a
# freshly-loaded packaged index is honoured. Overlay-ABSENT ⇒ effective == packaged (unchanged).
_criteria.register_gate(
    _GATE_KEY,
    packaged_index=lambda: routing_index(),
    canonical=lambda: frozenset(routing_index()),
)


def effective_routing(repo_root: str | None = None) -> dict[str, Any]:
    """The packaged routing index MERGED with a project ``.rebar/criteria_routing.json``
    overlay's ``code_review`` map (repo-keyed, cache-isolated). DELEGATES to the shared
    :func:`rebar.llm.criteria.effective_routing` with ``gate_key="code_review"`` (story 5065).
    Overlay-ABSENT ⇒ byte-identical to :func:`routing_index`."""
    return _criteria.effective_routing(repo_root, gate_key=_GATE_KEY)


def effective_criteria(repo_root: str | None = None) -> tuple[str, ...]:
    """The ACTIVE code-review criterion-id vocabulary = the packaged built-ins ∪ the project
    ids the overlay ``activate``s (minus any disabled built-in). DELEGATES to the shared
    :func:`rebar.llm.criteria.effective_criteria` with ``gate_key="code_review"`` (story 5065),
    so a project can add code-review criteria through the same overlay it uses for plan-review."""
    return _criteria.effective_criteria(repo_root, gate_key=_GATE_KEY)


# ── DET-criteria selectors (data-driven detector→criterion routing, story 7f0d) ──
# The code-review DET consumer (detectors.py) used to hardcode its detector→criterion map
# (a `rebar.builtin.security.` prefix + a gitleaks sentinel id). It now reads it FROM the
# routing index: every `exec: "DET"` entry may carry a `detector` selector ({"id": ...} for
# an EXACT match, {"id_prefix": ...} for a prefix class) + a per-criterion `fail_mode`
# ("open" | "closed"; default "open"). This is the generalization seam — a project can add
# its own DET invariant criterion + detector without touching the consumer code.


def det_criteria() -> dict[str, dict[str, Any]]:
    """The `exec: "DET"` routing entries as ``{criterion_id: {detector, fail_mode}}``.

    ``fail_mode`` defaults to ``"open"`` when absent (project invariants fail open — a coverage
    gap is recorded but does not block); the packaged security criteria ship ``"closed"``. An
    entry with no ``detector`` selector still appears (so the consumer records it) with
    ``detector=None``."""
    out: dict[str, dict[str, Any]] = {}
    for cid, entry in routing_index().items():
        if str((entry or {}).get("exec", "")).upper() != "DET":
            continue
        out[cid] = {
            "detector": (entry or {}).get("detector"),
            "fail_mode": (entry or {}).get("fail_mode", "open"),
        }
    return out


def criterion_for_detector(detector_id: str, det_map: dict[str, dict[str, Any]]) -> str | None:
    """Resolve the DET criterion a ``detector_id`` belongs to from a :func:`det_criteria` map.

    An EXACT ``detector.id`` match wins over a ``detector.id_prefix`` match — so the gitleaks
    sentinel routes to ``secret-detection`` while every OTHER ``rebar.builtin.security.*`` routes
    to ``high-critical-security`` (reproducing the former per-criterion detector logic
    exactly). Returns ``None`` when no selector matches (the detector is not one this
    consumer routes)."""
    prefix_hit: str | None = None
    for cid, spec in det_map.items():
        sel = spec.get("detector") or {}
        exact = sel.get("id")
        if exact is not None and detector_id == exact:
            return cid  # exact match wins outright
        pref = sel.get("id_prefix")
        if pref is not None and detector_id.startswith(pref):
            prefix_hit = prefix_hit if prefix_hit is not None else cid
    return prefix_hit


def applies_to_globs(criterion_id: str) -> list[str]:
    """The `applies_to` file globs for a criterion (the single source for WS3's Round-A
    glob-trigger logic). Empty list = escalation-only (no deterministic glob trigger)."""
    entry = routing_index().get(criterion_id) or {}
    globs = entry.get("applies_to") or []
    return [g for g in globs if isinstance(g, str)]


def _str_list(value: object) -> list[str]:
    """The ``str`` elements of ``value`` iff ``value`` is a list, else ``[]``. A plain STRING is
    ignored WHOLESALE (never iterated into single-character tokens) — a malformed
    ``trigger_tokens``/``applies_to`` must not silently trigger on stray characters."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def project_trigger_extensions(repo_root: str | None = None) -> dict[str, dict[str, list[str]]]:
    """The ADDITIVE trigger extensions a project overlay declares for BUILT-IN overlays.

    A project ``.rebar/criteria_routing.json`` ``code_review`` entry keyed by a member of
    :data:`OVERLAY_IDS` may carry ``trigger_tokens`` (literal substrings for
    :func:`content_triggered_overlays`) and ``applies_to`` (globs UNIONED into
    :func:`glob_triggered_overlays`). Read from the RAW overlay, NOT :func:`effective_routing`:
    the shared built-in merge is per-key overlay-wins (``{**merged[cid], **entry}``), so a
    project ``applies_to`` read from the merged view would REPLACE the committed globs — the
    opposite of the additive union this mechanism promises. Malformed values (non-list tokens,
    non-string elements, entries under an id not in :data:`OVERLAY_IDS`) are ignored, never
    raised. ``repo_root`` None / no overlay ⇒ ``{}``."""
    from rebar.llm.criteria.overlay import _load_overlay

    overlay = _load_overlay(repo_root)
    gate = (overlay or {}).get(_GATE_KEY)
    if not isinstance(gate, dict):
        return {}
    out: dict[str, dict[str, list[str]]] = {}
    for cid, entry in gate.items():
        if cid not in OVERLAY_IDS or not isinstance(entry, dict):
            continue
        tokens = _str_list(entry.get("trigger_tokens"))
        globs = _str_list(entry.get("applies_to"))
        if tokens or globs:
            out[cid] = {"trigger_tokens": tokens, "applies_to": globs}
    return out


def _glob_match(path: str, pattern: str) -> bool:
    """Match a changed-file path against an ``applies_to`` glob.

    Same rule as ``prompts._glob_match``: fnmatch over the full path, plus a ``**/`` prefix
    that also matches the bare suffix (so ``**/auth*`` matches a top-level ``auth.py``).
    Both delegate to the one implementation in ``_engine_support.commit_impact``.
    """
    from rebar._engine_support.commit_impact import glob_match

    return glob_match(path, pattern)


def glob_triggered_overlays(
    changed_files: Sequence[str], repo_root: str | None = None
) -> list[str]:
    """The overlays whose ``applies_to`` globs match ANY changed file — the deterministic
    Round-A trigger set (the ``glob`` operand of ``overlay_union``'s formula). Ordered by
    :data:`OVERLAY_IDS`. An escalation-only overlay (empty ``applies_to``) never glob-fires.

    Each overlay's committed globs are UNIONED with any project ``applies_to`` extension
    (:func:`project_trigger_extensions`); ``repo_root`` None / no overlay ⇒ committed globs only
    (byte-identical to before)."""
    ext = project_trigger_extensions(repo_root)
    out: list[str] = []
    for oid in OVERLAY_IDS:
        globs = applies_to_globs(oid) + ext.get(oid, {}).get("applies_to", [])
        if globs and any(_glob_match(f, g) for f in changed_files for g in globs):
            out.append(oid)
    return out


def project_applies_to_globs(criterion_id: str, repo_root: str | None = None) -> list[str]:
    """The ``applies_to`` file globs a PROJECT criterion declares, read from the
    overlay-MERGED routing.

    Deliberately NOT :func:`applies_to_globs`: that one reads the PACKAGED
    :func:`routing_index`, which carries no entry for a ``project.<name>`` id at all — so it
    would report an empty list for every project criterion and silently fail open. A project
    criterion exists only in ``.rebar/criteria_routing.json``, hence :func:`effective_routing`.
    """
    entry = effective_routing(repo_root).get(criterion_id) or {}
    globs = entry.get("applies_to") or []
    return [g for g in globs if isinstance(g, str)]


def project_criterion_applies(
    criterion_id: str, changed_files: Sequence[str], repo_root: str | None = None
) -> bool:
    """Whether a project criterion's ``applies_to`` admits this review's changed files.

    An EMPTY (or absent) glob list means UNGATED — the criterion runs on every review. That
    is the OPPOSITE of the same key's meaning on a BUILT-IN overlay, where empty means
    "escalation-only, never glob-fires" (:func:`glob_triggered_overlays`). The project meaning
    is forced: a ``project.`` id is not in :data:`OVERLAY_IDS`, so the base reviewer can never
    escalate to it and "empty = never run" would make every project criterion dead. The
    collision is documented in ADR 0074. ``applies_to: ["**"]`` is repository-wide and
    selects UNCONDITIONALLY — including for a review with NO changed files — via the shared
    :func:`rebar.llm.criteria.snapshot.select_project_applicability` rule (see ADR 0102), so
    the ``[]`` → ``["**"]`` overlay migration is behavior-preserving at that edge.
    """
    globs = project_applies_to_globs(criterion_id, repo_root)
    from rebar.llm.criteria.snapshot import select_project_applicability

    return select_project_applicability(globs, changed_files).applies


# ── Content triggers (the analog of glob triggers, keyed on the DIFF's removed lines) ────────
# Pragmatic, POLYGLOT removed-declaration patterns. This is deliberately a heuristic (the story
# expects it to EVOLVE per language): it matches a def/class/function/method SIGNATURE on a
# removed (`-`) diff line. Anchored at the (indentation-stripped) start of the line so a call or
# reference in the MIDDLE of a line never fires it.
_REMOVED_DECL_PATTERNS = (
    r"def\s+\w+\s*\(",  # Python function / method
    r"class\s+\w+",  # Python / JS / TS / Java / C++ class
    r"func\s+(?:\([^)]*\)\s*)?\w+\s*\(",  # Go function / method (optional receiver)
    r"fn\s+\w+",  # Rust function
    r"function\s*\*?\s*\w*\s*\(",  # JS / TS function (incl. generators / anonymous)
    # JS/TS arrow fn: `const x = (a) =>` (optional type annotations / async):
    r"(?:const|let|var)\s+\w+\s*(?::[^=]+?)?=\s*(?:async\s+)?\([^)]*\)\s*(?::\s*[^=]+?)?=>",
    r"(?:const|let|var)\s+\w+\s*=\s*(?:async\s+)?function",  # JS `const x = function`
)
_REMOVED_DECL_RE = re.compile(
    r"^\s*(?:export\s+|default\s+|public\s+|private\s+|protected\s+|static\s+|async\s+)*"
    r"(?:" + "|".join(_REMOVED_DECL_PATTERNS) + r")"
)

# Committed high-precision concurrency-race trigger tokens (distinct from the per-project
# `trigger_tokens` mechanism above). A literal substring on an added/removed diff line body
# fires the built-in `concurrency` overlay.
_CONCURRENCY_TOKENS: tuple[str, ...] = (
    "Mutex",
    "RwLock",
    "Lock(",
    "Semaphore",
    "synchronized",
    "volatile",
    "@GuardedBy",
    "Interlocked.",
    "Atomics.",
    "pthread_",
    "flock",
    "fcntl.",
    "O_EXCL",
    "mkstemp",
    "Thread(",
    "go func(",
    "tokio::spawn",
    "Task.Run(",
    "worker_threads",
    "new Worker(",
    "multiprocessing.",
    "asyncio.create_task",
    "asyncio.gather",
    "concurrent.futures",
    "os.fork(",
    "cluster.fork(",
    "sync.WaitGroup",
    "sync.Once",
    "make(chan ",
    "atomic.",
    "FOR UPDATE",
    "SKIP LOCKED",
    "SERIALIZABLE",
    "advisory_lock",
    "NOWAIT",
    "BEGIN IMMEDIATE",
    "start_new_session",
    "compare-and-swap",
    "coroutineScope",
    "launch {",
)


def content_triggered_overlays(diff_text: str, repo_root: str | None = None) -> list[str]:
    """The overlays triggered by the DIFF CONTENT (the ``content`` operand of ``overlay_union``,
    unioned alongside the glob triggers), ordered by :data:`OVERLAY_IDS`.

    Two scans, unioned: (1) the committed removed-declaration scan — a removed
    def/class/function-signature on a ``-`` line fires ``deletion-impact`` (skipping the ``---``
    file header); (2) any project ``trigger_tokens`` (:func:`project_trigger_extensions`) whose
    literal substring appears on an ADDED (``+``) or REMOVED (``-``) line body (the ``+++``/``---``
    headers excluded) fires that project-extended built-in overlay. ``repo_root`` None / no
    overlay ⇒ the committed deletion-impact behavior only (byte-identical to before): a pure
    add-only diff, a body-only edit that keeps the signature, and removed comment/blank lines
    all yield ``[]``."""
    if not diff_text:
        return []
    fired: set[str] = set()
    if _has_removed_declaration(diff_text):
        fired.add("deletion-impact")
    if _has_concurrency_token(diff_text):
        fired.add("concurrency")
    _scan_project_tokens(diff_text, project_trigger_extensions(repo_root), fired)
    return [oid for oid in OVERLAY_IDS if oid in fired]


def _has_removed_declaration(diff_text: str) -> bool:
    """Whether any removed (``-``) line — excluding the ``---`` file header — drops a
    def/class/function signature (the committed deletion-impact trigger)."""
    for raw in diff_text.splitlines():
        if not raw.startswith("-") or raw.startswith("---"):
            continue
        if _REMOVED_DECL_RE.search(raw[1:]):  # strip the diff marker; keep the indentation
            return True
    return False


def _has_concurrency_token(diff_text: str) -> bool:
    """Whether any added (``+``) or removed (``-``) line body — excluding the ``+++``/``---``
    file headers — contains a committed :data:`_CONCURRENCY_TOKENS` literal substring."""
    for raw in diff_text.splitlines():
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if not (raw.startswith("+") or raw.startswith("-")):
            continue
        body = raw[1:]
        if any(tok in body for tok in _CONCURRENCY_TOKENS):
            return True
    return False


def _scan_project_tokens(
    diff_text: str, ext: dict[str, dict[str, list[str]]], fired: set[str]
) -> None:
    """Add to ``fired`` every built-in overlay id whose project ``trigger_tokens`` literal
    substring appears on an added/removed diff line body (``+++``/``---`` headers excluded)."""
    token_map = {oid: e["trigger_tokens"] for oid, e in ext.items() if e.get("trigger_tokens")}
    if not token_map:
        return
    for raw in diff_text.splitlines():
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if not (raw.startswith("+") or raw.startswith("-")):
            continue
        body = raw[1:]
        for oid, tokens in token_map.items():
            if oid not in fired and any(tok in body for tok in tokens):
                fired.add(oid)


def overlay_flag_key(overlay_id: str) -> str:
    """The workflow-output flag key for an overlay (``security`` -> ``include_security``,
    ``db-migrations`` -> ``include_db_migrations``). Hyphens become underscores so the tiny
    ``${{ ... }}`` expression grammar reads it as a single identifier (no ``-`` = subtraction)."""
    return "include_" + overlay_id.replace("-", "_")


def threshold_for(
    criteria: Sequence[str], routing_map: dict[str, Any] | None = None
) -> tuple[float, bool]:
    """Resolve ``(block_threshold, blocking_enabled)`` for a finding's criteria — the
    ``ThresholdResolver`` the kernel ``pass3_over_findings(..., threshold_for=...)`` consumes.

    block_threshold = the MIN over the criteria's thresholds (most aggressive; default 0.95);
    blocking_enabled = True iff ANY criterion has ``blocking_enabled: true`` in the routing index
    (the field WS5 flips True for the secret-detection / high-critical-security keys). An unknown
    criterion contributes the default threshold and is NOT blocking — so a base-reviewer dimension
    with no routing entry stays advisory at 0.95.

    NOTE — intentional divergence from plan-review's resolver: that one DERIVES blocking from
    ``default_posture == "blocking"``; we read an EXPLICIT ``blocking_enabled`` field instead.
    This is deliberate — the detector keys are ``default_posture: "blocking"`` (their INTENDED
    posture) yet must ship ADVISORY in v1, which only a separate enable flag expresses; WS5 flips
    the flag without touching the posture. Since story 5065 BOTH conventions live SIDE-BY-SIDE in
    the shared :func:`rebar.llm.criteria.threshold_for`, dispatched on ``gate=`` — this function
    DELEGATES there with ``gate="code_review"`` (the divergence is preserved, not collapsed).

    ``routing_map`` defaults to the PACKAGED :func:`routing_index` (byte-identical to before); a
    repo-aware caller may pass :func:`effective_routing` to honour a project overlay's re-tunes."""
    idx = routing_map if routing_map is not None else routing_index()
    return _criteria.threshold_for(criteria, idx, gate=_GATE_KEY)


def nit_suppressed_criteria(routing_map: dict[str, Any] | None = None) -> frozenset[str]:
    """The set of criteria flagged ``nit_suppressed: true`` in the routing index (story
    grusome-uncheerful-nematode). ``code_review_decide`` demotes an advisory finding whose criteria
    are ALL in this set from the surfaced advisory set to ``dropped`` — reducing docs/llm-prompts
    nit noise without touching the blocking posture. ``routing_map`` defaults to the PACKAGED
    :func:`routing_index`; a repo-aware caller may pass :func:`effective_routing` to honour a
    project overlay's re-tunes (an overlay can add or clear the flag on any criterion)."""
    idx = routing_map if routing_map is not None else routing_index()
    return frozenset(k for k, v in idx.items() if isinstance(v, dict) and v.get("nit_suppressed"))
