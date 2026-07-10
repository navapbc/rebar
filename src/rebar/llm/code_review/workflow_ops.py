"""Scripted ops for the code-review gate workflow (epic b744 / WS3).

The deterministic glue between WS1's base reviewer, WS2's overlays + move-catalog, and the
shipped review kernel (Pass-2 verify / Pass-3 decide / Pass-4 coach). Mirrors
``plan_review/workflow_ops.py`` but for a DIFF (not a ticket plan): the two NOVEL ops are
``overlay_union`` (the base→overlay escalation: ``(glob ∪ recommend) − already_run``, one-hop,
capped) and ``merge_findings`` (concatenate + cluster the three finding sources). The rest are
the standard Pass wiring (assemble_diff / verify_inputs / decide / coach_inputs / coach), each a
thin consumer of the kernel — no forked passes.

Registered into the shared workflow STEP_REGISTRY via ``@register_step`` (imported from
``workflow/steps.py``, the same place plan_review's ops register).
"""

from __future__ import annotations

import logging
from typing import Any

from rebar.llm.workflow.executor import StepContext, register_step

logger = logging.getLogger(__name__)

_LINE_BUCKET = 10  # cluster findings within ~10 lines (mirrors aggregate.py's _LINE_BUCKET)


# ── assemble the diff context ──────────────────────────────────────────────────────────────
@register_step(
    "assemble_diff",
    input_schema="assemble_diff_input",
    output_schema="assemble_diff_output",
    description=(
        "Assemble the code-review kernel `context` string from the workflow's diff inputs "
        "(base/head range or a supplied unified diff + changed_files). Emits {context, "
        "changed_files} — the diff the base reviewer + overlays + Pass-2 verifier re-ground "
        "against, and the changed-files list overlay_union glob-matches."
    ),
)
def assemble_diff(ctx: StepContext) -> dict[str, Any]:
    from rebar.llm.code_review import assemble

    # The caller provides ALL declared inputs (the v3 engine errors on a referenced-but-unset
    # input), passing empty values where N/A. Coerce empty -> None so the assembler picks the
    # right mode: a non-empty diff_text is reviewed directly (changed_files derived from it when
    # omitted); otherwise the base..head git range is read.
    diff_text = ctx.inputs.get("diff_text") or None
    changed_files = ctx.inputs.get("changed_files") or None
    dc = assemble.assemble_diff_context(
        base=str(ctx.inputs.get("base") or "HEAD~1"),
        head=str(ctx.inputs.get("head") or "HEAD"),
        diff_text=diff_text,
        changed_files=changed_files,
        repo_root=ctx.repo_root,
        commit_message=str(ctx.inputs.get("commit_message") or ""),
    )
    # `scope_context` is the UNION scope/AC of the commit's rebar-ticket trailer tickets (empty
    # unless >=1 resolves). It is emitted as a SEPARATE output — NOT folded into `context` — so
    # base + every overlay but scope-intent stay ticket-blind. overlay_union reads it to gate the
    # scope-intent overlay; the per-overlay context_override that carries it into the scope-intent
    # prompt is wired by produce_code_review_verdict (CodeReviewBatchRunner.context_overrides).
    return {
        "context": dc.context,
        "changed_files": dc.changed_files,
        "scope_context": dc.scope_context,
    }


# ── the base→overlay escalation union (NOVEL) ───────────────────────────────────────────────
@register_step(
    "overlay_union",
    input_schema="overlay_union_input",
    output_schema="overlay_union_output",
    description=(
        "Compute the overlay inclusion set: (glob ∪ content ∪ base.recommend) − already_run, "
        "ONE-HOP, capped at N (configurable, default uncapped). `glob` = overlays whose applies_to "
        "globs match the changed files (registry.glob_triggered_overlays); `content` = overlays "
        "triggered by the DIFF CONTENT (registry.content_triggered_overlays, e.g. deletion-impact "
        "on a removed def/class/signature); `recommend` = the base reviewer's enum-validated "
        "recommend_overlays; `already_run` = the Round-A set (explicit with: input). scope-intent "
        "is the exception: included IFF `scope_context` (the assembler's resolved rebar-ticket "
        "trailer scope) is non-empty, never via glob/content/recommend. Emits "
        "include_<overlay> booleans (underscored ids) + to_run/glob_overlays/content_overlays/"
        "recommend_overlays. Called twice: as the Round-A `triggers` step (recommend/already_run "
        "default empty → to_run = glob ∪ content) and the Round-B `union` step (recommend=base, "
        "already_run=triggers.to_run, cap=N)."
    ),
)
def overlay_union(ctx: StepContext) -> dict[str, Any]:
    from rebar.llm.code_review import registry

    changed = list(ctx.inputs.get("changed_files") or [])
    diff_text = str(ctx.inputs.get("diff_text") or "")
    recommend = ctx.inputs.get("recommend") or []
    already_run = set(ctx.inputs.get("already_run") or [])
    cap = ctx.inputs.get("cap")
    scope_context = str(ctx.inputs.get("scope_context") or "")

    glob_set = registry.glob_triggered_overlays(changed)
    content_set = registry.content_triggered_overlays(diff_text)
    recommend_ids = registry.recommend_overlay_ids(recommend)
    selected = set(glob_set) | set(content_set) | set(recommend_ids)
    # scope-intent is TRAILER-driven, not glob/diff-content/recommend driven: it fires iff the
    # assembler resolved >=1 rebar-ticket trailer into a non-empty `scope_context`. Force its
    # membership to that signal ONLY (discard any stray glob/content/recommend selection) so an
    # over-eager base recommendation can never run it WITHOUT the ticket context it needs (which
    # would flag the whole diff as out-of-scope).
    selected.discard("scope-intent")
    if scope_context.strip():
        selected.add("scope-intent")
    # Ordered by OVERLAY_IDS (deterministic); minus the already-run set (one-hop bound: a
    # Round-A overlay never re-runs in Round-B).
    to_run = [o for o in registry.OVERLAY_IDS if o in selected and o not in already_run]
    if isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap >= 0:
        to_run = to_run[: int(cap)]
    out: dict[str, Any] = {
        registry.overlay_flag_key(o): (o in to_run) for o in registry.OVERLAY_IDS
    }
    out["to_run"] = to_run
    out["glob_overlays"] = glob_set
    out["content_overlays"] = content_set
    out["recommend_overlays"] = recommend_ids
    return out


# ── merge + cluster the three finding sources (NOVEL) ───────────────────────────────────────
def _norm_text(s: Any) -> str:
    return " ".join(str(s or "").lower().split())[:80]


def _parse_location(loc: Any) -> tuple[str | None, int | None]:
    """Best-effort (path, line) from a finding's ``location`` (e.g. ``path:line`` / ``path``)."""
    if not isinstance(loc, str) or not loc.strip():
        return (None, None)
    s = loc.strip()
    path, sep, rest = s.rpartition(":")
    if sep and path:
        head = rest.split("-", 1)[0].split(",", 1)[0].strip()
        try:
            return (path, int(head))
        except ValueError:
            return (s, None)
    return (s, None)


def _cluster_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse near-duplicate kernel findings to bound the Pass-2 budget — the aggregate.py
    clustering idea (file-anchor + dimension + line-proximity) adapted to the kernel finding
    shape (location/criteria, not citations/dimension). Clustering keys on (location-path,
    primary criterion, line-proximity within ~10 lines); a finding with NO location OR NO
    criterion is clustered by (criterion, normalized finding-text) instead, so two distinct
    issues that merely lack a location/criterion are NOT collapsed by coincidence.

    The representative is the first finding; the cluster's evidence is unioned, ``agreement`` =
    cluster size, ``reviewers`` records the overlays that raised it, and — so collapsing is
    NON-LOSSY for recall — the OTHER members' finding text is preserved in ``merged_from``
    (Pass-2 verifies the representative, but the verdict/sidecar can still surface the rest)."""
    clusters: list[dict[str, Any]] = []
    for f in findings:
        crit = (f.get("criteria") or [""])[0] if f.get("criteria") else ""
        path, line = _parse_location(f.get("location"))
        txt = _norm_text(f.get("finding"))
        # Location-anchored clustering needs BOTH a path and a non-empty criterion; otherwise
        # fall back to (criterion, text) so we never collapse two distinct findings that merely
        # share a location with no criterion (or no location at all).
        anchored = path is not None and bool(crit)
        placed = False
        for c in clusters:
            if c["crit"] != crit:
                continue
            if anchored and c["anchored"] and c["path"] == path:
                if line is None or c["line"] is None:
                    match = line is None and c["line"] is None
                else:
                    match = abs(line - c["line"]) <= _LINE_BUCKET
            elif not anchored and not c["anchored"]:
                match = c["txt"] == txt
            else:
                match = False
            if match:
                c["members"].append(f)
                placed = True
                break
        if not placed:
            clusters.append(
                {
                    "path": path,
                    "line": line,
                    "crit": crit,
                    "txt": txt,
                    "anchored": anchored,
                    "members": [f],
                }
            )
    out: list[dict[str, Any]] = []
    for c in clusters:
        rep = dict(c["members"][0])
        # Coerce the load-bearing fields to safe types: the findings-items schema is permissive
        # (additionalProperties, no per-field type), so an LLM payload can carry `finding: null`
        # / a non-string, or `criteria: null`. coach_listing does `f['finding'][:200]` and the
        # kernel iterates `criteria` — both would crash. Normalize (not just default-if-missing).
        rep["finding"] = str(rep.get("finding") or "")
        rep["criteria"] = [c2 for c2 in (rep.get("criteria") or []) if isinstance(c2, str)]
        evidence: list[str] = list(rep.get("evidence") or [])
        reviewers: list[str] = []
        merged_from: list[str] = []
        for j, m in enumerate(c["members"]):
            for e in m.get("evidence") or []:
                if e not in evidence:
                    evidence.append(e)
            rid = m.get("reviewer_id")
            if rid and rid not in reviewers:
                reviewers.append(rid)
            if j > 0 and m.get("finding"):  # preserve the collapsed members' text (non-lossy)
                merged_from.append(str(m["finding"]))
        rep["evidence"] = evidence
        rep["agreement"] = len(c["members"])
        if merged_from:
            rep["merged_from"] = merged_from
        if reviewers:
            rep["reviewers"] = reviewers
        out.append(rep)
    return out


@register_step(
    "merge_findings",
    input_schema="merge_findings_input",
    output_schema="merge_findings_output",
    description=(
        "Concatenate the base + Round-A + Round-B finding lists (provenance preserved via each "
        "finding's reviewer_id), CLUSTER near-duplicates (same location+criterion within ~10 "
        "lines, or same criterion+text for location-less findings) so N overlays on one spot "
        "don't inflate the Pass-2 budget, and assign a stable 0-based `id` per finding. Emits "
        "{findings, merged_count, clustered_count}."
    ),
)
def merge_findings(ctx: StepContext) -> dict[str, Any]:
    sources = ctx.inputs.get("sources")
    if sources is None:
        sources = [
            ctx.inputs.get("base_findings"),
            ctx.inputs.get("round_a_findings"),
            ctx.inputs.get("round_b_findings"),
        ]
    collected: list[dict[str, Any]] = []
    for src in sources:
        for f in src or []:
            if isinstance(f, dict):
                collected.append(dict(f))
    clustered = _cluster_findings(collected)
    for i, f in enumerate(clustered):
        f["id"] = str(i)
    return {
        "findings": clustered,
        "merged_count": len(collected),
        "clustered_count": len(clustered),
    }


# ── Pass-2 verify inputs (the finding listing for the verify prompt) ────────────────────────
@register_step(
    "code_review_verify_inputs",
    input_schema="code_review_verify_inputs_input",
    output_schema="code_review_verify_inputs_output",
    description=(
        "Build the Pass-2 verifier prompt's instructions: the kernel finding-listing over the "
        "merged findings (one aggregate pass). Emits {instructions}."
    ),
)
def code_review_verify_inputs(ctx: StepContext) -> dict[str, Any]:
    from rebar.llm import review_kernel

    findings = list(ctx.inputs.get("findings") or [])
    instructions = review_kernel.verify_instructions(list(enumerate(findings)))
    return {"instructions": instructions}


# ── Pass-3 DET enrichment for impact_code (story albite-lazy-barb) ──────────────────────────
# decide.impact_code reads two DET signals decide.py cannot compute itself: `churn90` (how hot
# the file is) and `hard_to_reverse_surface` (a one-way-door file). We compute them here — from
# each finding's `location` path + the diff — and inject them into the VERIFICATION dict's
# `severity_attributes` (the exact dict pass3_decide passes to impact_code; NOT the finding
# dict). Best-effort: any failure leaves the signal at its safe default (churn 0 ⇒ freq_mult 0.5;
# surface False ⇒ no reversibility floor).
_PACKAGING_BASENAMES = {"pyproject.toml", "setup.py", "setup.cfg"}
_SERIALIZATION_EXTS = {".proto", ".sql"}


def _file_from_location(location: str) -> str:
    """Extract the file path from a finding `location` ('path' or 'path:line[:col]')."""
    loc = (location or "").strip()
    if not loc:
        return ""
    # Strip a trailing ':line' (and ':col'); a bare path with no ':' passes through unchanged.
    return loc.split(":", 1)[0].strip()


def _hard_to_reverse_surface(path: str, deleted: set[str]) -> bool:
    """A one-way-door surface: released packaging, a serialization/schema artifact, or a deletion.
    An exported-public-API break is undetectable from the path alone, so we conservatively do NOT
    floor on it (the impact_base>0 gate in impact_code means a clean finding never gets floored)."""
    from pathlib import PurePosixPath

    if not path:
        return False
    if path in deleted:
        return True
    p = PurePosixPath(path)
    base = p.name
    if base in _PACKAGING_BASENAMES or "CHANGELOG" in base:
        return True
    if p.suffix.lower() in _SERIALIZATION_EXTS:
        return True
    if base.endswith(".schema.json") or (base.startswith("schema") and base.endswith(".json")):
        return True
    return False


def _churn_90d(path: str, repo_root: Any) -> int:
    """Commits touching `path` in the last 90 days (best-effort; 0 on any failure)."""
    import subprocess

    if not path or not repo_root:
        return 0
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "log", "--since=90 days ago", "--oneline", "--", path],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if out.returncode != 0:
        return 0
    return sum(1 for line in out.stdout.splitlines() if line.strip())


def _deleted_paths_from_diff(diff_text: str) -> set[str]:
    """Files removed in a unified diff (a '+++ /dev/null' hunk header names a deletion)."""
    deleted: set[str] = set()
    lines = (diff_text or "").splitlines()
    for i, line in enumerate(lines):
        if line.startswith("+++ ") and line[4:].strip() in {"/dev/null", "b/dev/null"}:
            for j in range(i - 1, max(-1, i - 4), -1):
                if lines[j].startswith("--- "):
                    p = lines[j][4:].strip()
                    if p.startswith("a/"):
                        p = p[2:]
                    if p and p != "/dev/null":
                        deleted.add(p)
                    break
    return deleted


def _det_enrich_verifications(
    findings: list[dict[str, Any]],
    verifications: dict[int, dict[str, Any]],
    *,
    diff_text: str,
    repo_root: Any,
) -> None:
    """Inject `churn90` + `hard_to_reverse_surface` into each verification's `severity_attributes`
    (in place) so impact_code reads DET signals alongside the LLM binaries from ONE dict."""
    deleted = _deleted_paths_from_diff(diff_text)
    churn_cache: dict[str, int] = {}
    for i, f in enumerate(findings):
        verif = verifications.get(i)
        if not isinstance(verif, dict):
            continue
        attrs = verif.get("severity_attributes")
        if not isinstance(attrs, dict):
            attrs = {}
            verif["severity_attributes"] = attrs
        path = _file_from_location(str(f.get("location", "")))
        if path not in churn_cache:
            churn_cache[path] = _churn_90d(path, repo_root)
        attrs["churn90"] = churn_cache[path]
        attrs["hard_to_reverse_surface"] = _hard_to_reverse_surface(path, deleted)


# ── Pass-3 decide (deterministic, kernel) ───────────────────────────────────────────────────
@register_step(
    "code_review_decide",
    input_schema="code_review_decide_input",
    output_schema="code_review_decide_output",
    description=(
        "Pass-3: reshape the Pass-2 verifier's flat verifications to the {index: verification} "
        "map, run the kernel pass3_over_findings with the code-review threshold_for resolver, and "
        "partition by decision. Emits {decided, blocking, surfaced, dropped, indeterminate}. The "
        "decision is DETERMINISTIC — escalation (which overlays ran) can never change it."
    ),
)
def code_review_decide(ctx: StepContext) -> dict[str, Any]:
    from rebar.llm import review_kernel
    from rebar.llm.code_review import registry

    findings = list(ctx.inputs.get("findings") or [])
    raw_verifs = list(ctx.inputs.get("verifications") or [])
    reshape = review_kernel.reshape_verifications(raw_verifs, valid_indices=range(len(findings)))
    if reshape.has_violations:
        logger.error(
            "code-review Pass-2 verification contract violation (findings degrade to "
            "INDETERMINATE; verdict unchanged): %s",
            reshape.summary(),
        )
    # DET-enrich the verifications' severity_attributes (churn90 + hard_to_reverse_surface) so
    # decide.impact_code reads DET signals alongside the LLM binaries from the SAME dict.
    _det_enrich_verifications(
        findings,
        reshape.verifications,
        diff_text=str(ctx.inputs.get("diff_text") or ""),
        repo_root=ctx.repo_root,
    )
    decided = review_kernel.pass3_over_findings(
        findings,
        reshape.verifications,
        threshold_for=registry.threshold_for,
        impact_fn=review_kernel.impact_code,
    )
    # Nit-suppression (story grusome-uncheerful-nematode): an ADVISORY finding whose criteria are
    # ALL flagged nit_suppressed in the routing (docs / llm-prompts) is demoted from surfaced to
    # dropped so it adds no coaching noise. POST-pass3: partition-only — validity/impact/priority
    # and every BLOCK decision are untouched; a finding that ALSO maps to a non-suppressed
    # criterion still surfaces (all-criteria rule).
    nit_suppressed = registry.nit_suppressed_criteria()
    buckets: dict[str, list[dict[str, Any]]] = {
        "blocking": [],
        "surfaced": [],
        "dropped": [],
        "indeterminate": [],
    }
    for f in decided:
        decision = f.get("decision")
        if decision == "block":
            buckets["blocking"].append(f)
        elif decision == "advisory":
            crit = f.get("criteria") or []
            if crit and all(c in nit_suppressed for c in crit):
                f["decision"] = "dropped"
                f["reason"] = "nit-suppressed"
                buckets["dropped"].append(f)
            else:
                buckets["surfaced"].append(f)
        elif decision == "dropped":
            buckets["dropped"].append(f)
        else:
            buckets["indeterminate"].append(f)
    return {"decided": decided, **buckets}


# ── Pass-4 coach inputs (the move-pick listing for the coach prompt) ────────────────────────
@register_step(
    "code_review_coach_inputs",
    input_schema="code_review_coach_inputs_input",
    output_schema="code_review_coach_inputs_output",
    description=(
        "Build the Pass-4 coach prompt's instructions: the kernel coach-listing over the "
        "surviving (surfaced) advisory findings + the APPLICABLE code moves (those whose "
        "applies_when overlaps the surviving findings' criteria). Emits {instructions, "
        "has_surviving}."
    ),
)
def code_review_coach_inputs(ctx: StepContext) -> dict[str, Any]:
    from rebar.llm import review_kernel
    from rebar.llm.code_review import moves

    surfaced = list(ctx.inputs.get("surfaced") or [])
    mr = moves.load_move_registry(ctx.repo_root)
    triggers = {c for f in surfaced for c in f.get("criteria", []) or []}
    applicable = review_kernel.applicable_moves(mr, triggers)
    instructions = review_kernel.coach_listing(surfaced, applicable)
    return {"instructions": instructions, "has_surviving": bool(surfaced)}


# ── Pass-4 coach render + verdict assembly (terminal output) ────────────────────────────────
@register_step(
    "code_review_coach",
    input_schema="code_review_coach_input",
    output_schema="code_review_coach_output",
    description=(
        "Pass-4 render + verdict assembly: render the coach prompt's raw move-picks into "
        "deterministic coaching (locked move templates; the LLM never authors prose), over the "
        "SAME applicable-move subset the prompt picked among, then assemble the terminal "
        "code-review verdict {verdict (BLOCK iff any blocking finding else PASS), blocking, "
        "advisory, coaching, coverage}. NO signing/sidecar here (that is WS4's produce_"
        "code_review_verdict)."
    ),
)
def code_review_coach(ctx: StepContext) -> dict[str, Any]:
    from rebar.llm import review_kernel
    from rebar.llm.code_review import moves

    blocking = list(ctx.inputs.get("blocking") or [])
    surfaced = list(ctx.inputs.get("surfaced") or [])
    raw_notes = list(ctx.inputs.get("notes") or [])
    mr = moves.load_move_registry(ctx.repo_root)
    triggers = {c for f in surfaced for c in f.get("criteria", []) or []}
    applicable = review_kernel.applicable_moves(mr, triggers)
    coaching = review_kernel.render_coach_notes(raw_notes, applicable)
    coverage = ctx.inputs.get("coverage") or {"llm_ran": True}
    return {
        "verdict": "BLOCK" if blocking else "PASS",
        "blocking": blocking,
        "advisory": surfaced,
        "coaching": coaching,
        "coverage": coverage,
    }


# ── region-gated novelty rising floor (story blameless-grindable-noctule) ─────────────────────────
def score_code_novelty(
    findings: list[dict[str, Any]],
    prior_findings: list[dict[str, Any]],
    *,
    diff_text: str,
    cfg: Any,
    runner: Any,
) -> dict[int, tuple[float, str]]:
    """Score each CURRENT finding's novelty against the prior SURFACED findings via the code-novelty
    sub-call, returning ``{index: (novelty ∈ [0,1], matched_prior_id)}``.

    Mirrors plan-review's ``_score_floor_novelty`` runner wiring — the prior findings reach ONLY the
    sub-call INSTRUCTIONS (never the Pass-1 finder), the diff is the domain context — but also
    returns the
    ``matched_prior_id`` too (for ``carried_from``), which the kernel ``score_novelty`` wrapper
    discards. It REUSES the same primitives that wrapper uses: the ``code_review_novelty`` output
    contract (the SAME ``novelty_model``), ``verify.reshape_novelties``, and ``decide.novelty`` — so
    the scoring is byte-identical, only the return shape is richer.

    FAIL-SAFE: no findings / no prior / any error → ``{}`` (every finding then scores 0.0, i.e.
    carryover
    = kept). A broken novelty signal can only make the floor keep MORE, never drop wrongly."""
    if not findings or not prior_findings:
        return {}
    try:
        from rebar.llm.prompting import prompts as _prompts
        from rebar.llm.review_kernel import decide, verify
        from rebar.llm.runner import RunRequest, get_runner

        runner_sel = runner or get_runner(cfg)
        prompt = _prompts.get_prompt("code-review-novelty", repo_root=cfg.repo_path)
        system, _meta = _prompts.resolve_prompt(prompt, {}, repo_root=cfg.repo_path)
        system = _prompts.strip_volatile_marker(system)
        batch = list(enumerate(findings))
        context = verify.prior_findings_block(prior_findings)
        req = RunRequest(
            system_prompt=system,
            instructions=(
                f"## Diff under review\n{diff_text}\n\n"
                f"{verify.novelty_instructions(batch)}\n\n"
                f"## Prior-review findings (context)\n{context}"
            ),
            config=cfg,
            reviewers=["code-novelty"],
            mode="structured",
            output_schema="code_review_novelty",
            execution_mode="single_turn",
        )
        raw = runner_sel.run(req).get("novelties", []) or []
        reshaped = verify.reshape_novelties(raw, valid_indices=range(len(findings)))
        out: dict[int, tuple[float, str]] = {}
        for gi in range(len(findings)):
            entry = reshaped.get(gi, {})
            out[gi] = (
                decide.novelty(entry.get("matches_prior", {})),
                str(entry.get("matched_prior_id") or ""),
            )
        return out
    except Exception:  # noqa: BLE001 — fail-safe: any error → un-floored (no drops)
        logger.warning("code-review novelty scoring failed; running un-floored", exc_info=True)
        return {}


def apply_region_gated_floor(
    verdict: dict[str, Any],
    *,
    key: str | None,
    cfg: Any,
    runner: Any,
    repo_root: Any = None,
    diff_text: str = "",
) -> None:
    """The region-gated novelty rising floor for code review — the convergence mechanism (epic
    super-path-bag). Mutates ``verdict`` IN PLACE, narrowing its ``advisory`` set before the sidecar
    emit (the code-review analogue of plan-review's ``_maybe_apply_rising_floor``).

    An advisory finding is DROPPED iff it is NOVEL (novelty ≥ ``novelty_drop_threshold``) AND
    low-priority (< ``novelty_priority_floor``) AND its cited region is ``REGION_UNCHANGED``. A
    ``REGION_CHANGED`` or ``REGION_UNKNOWN`` region ALWAYS raises (the fail-safe direction). Dropped
    findings move to ``verdict["dropped"]`` with ``drop_reason = "novelty-region"``. A finding that
    MATCHES a prior surfaced finding (low novelty) but is not dropped is stamped
    ``carried_from = matched_prior_id`` and has its coaching stripped, while remaining surfaced.

    REUSES ``decide.rising_floor_drop`` UNCHANGED and the c639 reader. Gated on
    ``verify.novelty_drop_active`` (the shared evidence gate, OFF by default — same three keys
    plan-review uses, no NEW config) and self-gates inert when there is no prior memory for ``key``.
    FAIL-SAFE: wrapped in try/except → any error leaves the verdict fully unfiltered (no drops)."""
    try:
        from rebar import config as _config
        from rebar.llm.code_review import region_gate, sidecar
        from rebar.llm.review_kernel import decide

        if not key:
            return
        vcfg = _config.load_config(repo_root).verify
        if not vcfg.novelty_drop_active:  # the shared evidence gate (off by default)
            return
        advisory = verdict.get("advisory") or []
        if not advisory:
            return
        prior = sidecar.latest_code_review_result(key, repo_root=repo_root)
        prior_findings = (prior or {}).get("findings") or []
        prior_deps = (prior or {}).get("deps") or {}
        if not prior_findings:  # self-gate: no prior memory ⇒ inert (no flag needed)
            return
        nmap = score_code_novelty(
            advisory, prior_findings, diff_text=diff_text, cfg=cfg, runner=runner
        )
        t_novel = vcfg.novelty_drop_threshold
        floor = vcfg.novelty_priority_floor
        kept: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = list(verdict.get("dropped") or [])
        carried_ids: set[Any] = set()
        for i, f in enumerate(advisory):
            nov, matched = nmap.get(i, (0.0, ""))
            prio = float(f.get("priority") or 0.0)
            region = region_gate.region_for_finding(f, prior_deps, repo_root=repo_root)
            drop = (
                decide.rising_floor_drop(prio, nov, t_novel=t_novel, floor=floor)
                and region == region_gate.REGION_UNCHANGED
            )
            if drop:
                dropped.append(
                    {**f, "decision": "dropped", "drop_reason": "novelty-region", "novelty": nov}
                )
                continue
            if matched and nov < t_novel:  # carryover: matches a prior surfaced finding, not novel
                f = {**f, "carried_from": matched}
                if f.get("id") is not None:
                    carried_ids.add(f.get("id"))
            kept.append(f)
        verdict["advisory"] = kept
        if dropped:
            verdict["dropped"] = dropped
        # Carryover is NOT re-coached: strip coaching notes that reference a carried finding (they
        # remain SURFACED, just uncoached). Findings carry an `id` by the coach stage.
        if carried_ids and verdict.get("coaching"):
            verdict["coaching"] = [
                c
                for c in verdict["coaching"]
                if not (set(c.get("finding_refs") or []) & carried_ids)
            ]
    except Exception:  # noqa: BLE001 — fail-safe: any error leaves the verdict unfiltered (no drops)
        logger.warning("region-gated floor failed; leaving verdict unfiltered", exc_info=True)
