"""Tier-2: replay Pass-1 criteria on a fixed stratified sample (ticket
peaceable-choppy-sapsucker / f1d6-cfcc-a9c6-416d), live over the pinned Bedrock
frontier-class model (Opus), budgeted.

**Execution tier is per-criterion.** ``registry.exec_tier`` returns only ``AGENT`` /
``2-STEP`` / ``1-TURN`` -- there is no ``CONTAINER`` value. ``production_batch_runner
._resolve_criteria`` splits by ``exec_tier == "AGENT"``; ``pass1.run_pass1`` then pulls
the container criteria (``pass1.CONTAINER_CRITERIA``, i.e. ``("G3", "G4",
"decomp-shape")``) out of that agent bucket itself and routes them to the dedicated
per-child loop. This module never re-implements that split for DISPATCH -- calling
``ProductionBatchRunner.run`` already resolves single/agent/container correctly end to
end. :func:`resolve_criteria_tiers` mirrors the split ONLY for cost-tier reporting
(so a report can label a criterion's real tier without duplicating dispatch).

**Candidate scope.** A candidate directory's ``.rebar/`` subtree (prompts +
``criteria_routing.json`` overlay -- the SAME override convention production reads) is
installed into the pinned ephemeral config root before a run, mirroring Tier-1's
``VerifierCandidate`` mechanism exactly. This covers PROMPT/THRESHOLD/POSTURE edits to
criteria ALREADY IN the live registry (the common edit shape). A candidate that
introduces a wholly NEW criterion id is OUT OF SCOPE: ``ProductionBatchRunner``'s
internal ``_resolve_criteria`` resolves each requested id against the LIVE default
registry (``registry.by_id()``, no repo_root override), so an unknown id is silently
skipped rather than routed to the candidate's own definition.

**Verdict chaining.** A fresh candidate Pass-1 finding carries no stored verification,
so it is verified live via the SAME :func:`verify_findings` seam Tier-1 uses, then
:func:`rebar.llm.evals.plan_replay.tier0.live_baseline_decisions` (a thin wrapper over
``orchestrator.pass3_over_findings``, called AS-IS) computes its verdict -- compared
against the review's ALREADY-STORED verdict (``f.get("decision")`` on the stored
findings), never re-derived.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from rebar.llm.evals.plan_replay import corpus, ledger, parity
from rebar.llm.evals.plan_replay.sampling import stratified_sample
from rebar.llm.evals.plan_replay.tier0 import (
    _load_cache_rows,
    build_event_index,
    execution_review_for,
    live_baseline_decisions,
    sidecar_data_for_row,
)
from rebar.llm.plan_review import det_floor, sizing
from rebar.llm.plan_review.container_stage import CONTAINER_CRITERIA
from rebar.llm.plan_review.production_batch_runner import ProductionBatchRunner, _resolve_criteria
from rebar.llm.plan_review.registry import CANONICAL_DET
from rebar.llm.plan_review.sidecar import norm_id
from rebar.llm.review_kernel.verify import verify_findings
from rebar.llm.workflow.runners import BatchRunRequest

_TIER = "tier2"
_PROMPT_ID_PREFIX = "plan-review-"
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


# ── sampling pool + fixed-sample I/O ────────────────────────────────────────────────
def build_sampling_pool(
    store_roots: dict[str, str],
    *,
    cache_dir: Path | str,
) -> list[dict[str, Any]]:
    """Corpus rows eligible for Tier-2 sampling: ``verified`` rows with a non-empty
    stored finding set, enriched with the full-body re-read's sidecar data (a corpus
    cache row is summary-only). UNLIKE Tier-1, this is not filtered by
    ``provider_provenance.ran_model``: that field records whichever pass last touched
    the review's overall provenance (empirically always the standard class in this
    corpus, since no row records a frontier-class ``ran_model`` -- Pass-1's per-call
    model is not separately persisted per finding), so a frontier-model filter would
    empty the pool. Tier-2 samples the same ``verified`` population Tier-0 replays."""
    manifest = corpus.build_corpus(store_roots, cache_dir=cache_dir)
    rows = _load_cache_rows(Path(cache_dir), manifest["content_hash"])
    event_index = build_event_index(store_roots)

    pool: list[dict[str, Any]] = []
    for row in rows:
        if not row.get("verified"):
            continue
        data = sidecar_data_for_row(row, event_index, store_roots)
        if data is None:
            continue
        findings = data.get("findings")
        if not isinstance(findings, list) or not findings:
            continue
        pool.append(
            {
                **row,
                "finding_count": len(findings),
                "sidecar_data": data,
            }
        )
    return pool


def draw_fixed_sample(pool: list[dict[str, Any]], *, n: int = 20, seed: int = 0) -> list[dict]:
    """Draw the ONE-TIME fixed Tier-2 sample from ``pool`` via Tier-1's stratified
    sampler (reused, not reinvented)."""
    return stratified_sample(pool, n=n, seed=seed)


def sample_entries(sample: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The persisted ``tier2-sample.json`` shape: the identity triple only (no
    sidecar body -- the sample file names WHICH reviews, not their content)."""
    return [
        {
            "store": r["store"],
            "ticket_id": r["ticket_id"],
            "review_event_uuid": r["review_event_uuid"],
        }
        for r in sample
    ]


def load_sample_file(path: str | Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def resolve_sample_against_pool(
    entries: list[dict[str, str]], pool: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Resolve the persisted sample's identity triples against the CURRENT pool,
    matched by ``(store, ticket_id, review_event_uuid)``. Raises ``ValueError`` naming
    any entry no longer present in the corpus (a manifest/sample drift) rather than
    silently shrinking the sample."""
    by_key = {(r["store"], r["ticket_id"], r["review_event_uuid"]): r for r in pool}
    resolved: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for entry in entries:
        key = (entry["store"], entry["ticket_id"], entry["review_event_uuid"])
        row = by_key.get(key)
        if row is None:
            missing.append(entry)
            continue
        resolved.append(row)
    if missing:
        raise ValueError(f"tier2 sample entries no longer resolvable against the corpus: {missing}")
    return resolved


def strata_summary(sample: list[dict[str, Any]]) -> dict[str, int]:
    """A quick strata census for the sample-file cross-check: container count, count
    with >=10 findings, count per store."""
    containers = sum(1 for r in sample if r.get("children"))
    heavy = sum(1 for r in sample if int(r.get("finding_count") or 0) >= 10)
    per_store: dict[str, int] = {}
    for r in sample:
        store = str(r.get("store"))
        per_store[store] = per_store.get(store, 0) + 1
    return {
        "containers": containers,
        "heavy": heavy,
        **{f"store:{k}": v for k, v in per_store.items()},
    }


# ── reconstructed-material scratch tracker (candidate Pass-1 must never see the ────
# live, CURRENT ticket -- only the plan text as it existed at the corpus review's time,
# same population Tier-0/Tier-1's own comparisons already hold fixed) ────────────────
def _reconstruct_child(
    events_by_ticket: dict[str, dict[str, list[dict[str, Any]]]],
    child_id: str,
    review_ts: int,
) -> dict[str, Any] | None:
    """One child's reconstructed material as of ``review_ts`` -- ``None`` when no
    recoverable CREATE event exists for it (skipped, never fabricated). ``title`` isn't
    tracked by ``corpus._reconstruct_material`` (only ``description``/``ticket_type``/
    ``file_impact`` are), so it's read directly off the CREATE event body here."""
    events = events_by_ticket.get(child_id)
    if not events:
        return None
    creates = events.get("CREATE", [])
    if not creates:
        return None
    title = creates[0]["data"].get("title", "")
    ttype, description, file_impact, _scope, _reason, _children, create_found = (
        corpus._reconstruct_material(events, review_ts)
    )
    if not create_found:
        return None
    return {
        "title": title,
        "ticket_type": ttype,
        "description": description,
        "file_impact": file_impact,
    }


def materialize_reconstructed_ticket(
    row: dict[str, Any], tracker_path: str, *, ticket_repo_root: str
) -> tuple[str, str]:
    """Materialize ``row``'s reconstructed-at-review-time material (ticket_type,
    description, file_impact, direct children) into a FRESH, throwaway scratch
    tracker, and return ``(scratch_repo_root, target_ticket_id)`` for
    ``ProductionBatchRunner`` to review. NEVER points at the real ticket store's live
    state -- the ticket's own approved plan requires candidate Pass-1 to see
    "reconstructed material", not whatever the real ticket looks like today (which may
    have drifted, or even closed, since the corpus review that produced the STORED
    findings being compared against).

    The scratch root is a real ``git worktree`` of ``ticket_repo_root`` at ``HEAD``
    (not a bare empty repo) so ``ctx.repo_root``/``resolve_code_root``'s explicit-
    override rule -- which ``assemble_context``'s single ``repo_root`` parameter feeds
    for BOTH the ticket read and the code-grounding root, with no seam to split them --
    still resolves to REAL project source for code-grounded criteria (G3/G4/T8/etc),
    not an empty scratch dir. A fresh ticket tracker is then mounted onto that worktree
    for the reconstructed ticket. This scratch tracker is disposable scaffolding, never
    the real project tracker -- the "no ad-hoc raw git in the tickets tracker" policy
    governs writes to THAT store, not this throwaway one. The caller owns cleanup via
    :func:`cleanup_reconstructed_ticket` (``git worktree remove``, not a bare
    ``shutil.rmtree``, so the main repo's worktree registration is not left dangling)."""
    import tempfile

    import rebar

    scratch_root = tempfile.mkdtemp(prefix="tier2-scratch-")
    shutil.rmtree(scratch_root)  # `git worktree add` requires the target to not exist
    subprocess.run(
        ["git", "worktree", "add", scratch_root, "HEAD", "--detach", "-q"],
        cwd=ticket_repo_root,
        check=True,
    )
    rebar.init_repo(repo_root=scratch_root, force_new_store=True)

    events_by_ticket = corpus._load_ticket_events(tracker_path)
    target_events = events_by_ticket.get(row["ticket_id"], {})
    creates = target_events.get("CREATE", [])
    title = creates[0]["data"].get("title", "") if creates else ""

    created = rebar.create_ticket(
        row["ticket_type"],
        title,
        description=row["description"],
        repo_root=scratch_root,
        return_alias=True,
    )
    target_id = created["id"]
    if row.get("file_impact"):
        rebar.set_file_impact(target_id, row["file_impact"], repo_root=scratch_root)

    for child_id in row.get("children") or []:
        child = _reconstruct_child(events_by_ticket, child_id, row["review_event_ts"])
        if child is None:
            continue
        child_created = rebar.create_ticket(
            child["ticket_type"],
            child["title"],
            description=child["description"],
            parent=target_id,
            repo_root=scratch_root,
            return_alias=True,
        )
        if child["file_impact"]:
            rebar.set_file_impact(child_created["id"], child["file_impact"], repo_root=scratch_root)

    return scratch_root, target_id


def cleanup_reconstructed_ticket(scratch_root: str, *, ticket_repo_root: str) -> None:
    """Tear down a :func:`materialize_reconstructed_ticket` scratch root. Uses ``git
    worktree remove`` (never a bare ``shutil.rmtree``) so the real repo's worktree
    registration (``$GIT_DIR/worktrees/``) is not left dangling for every future
    ``git worktree list`` in this repo."""
    result = subprocess.run(
        ["git", "worktree", "remove", "--force", scratch_root],
        cwd=ticket_repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        shutil.rmtree(scratch_root, ignore_errors=True)
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=ticket_repo_root,
            capture_output=True,
            text=True,
            check=False,
        )


# ── execution-tier resolution (reporting only -- dispatch is ProductionBatchRunner's) ──
def resolve_criteria_tiers(
    criteria: tuple[dict[str, Any], ...],
) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """``(single, agent, container, skipped)`` -- mirrors ``pass1.run_pass1``'s own
    container pull from ``_resolve_criteria``'s agent bucket, for REPORTING/cost-tier
    labeling only. Actual dispatch never calls this: ``ProductionBatchRunner.run``
    resolves single/agent/container correctly internally via ``run_pass1`` itself."""
    single, agent, skipped = _resolve_criteria(criteria)
    container = [c for c in agent if c["id"] in CONTAINER_CRITERIA]
    agent = [c for c in agent if c["id"] not in CONTAINER_CRITERIA]
    return single, agent, container, skipped


def single_criterion_request_criteria(criterion_id: str) -> tuple[dict[str, Any], ...]:
    """The single-entry ``req.criteria`` tuple for ``--criteria <id>`` -- the exact
    mechanism ``_resolve_criteria`` filters on (NOT ``probe_criteria``, which only
    feeds ``project.*`` fan-in)."""
    return ({"prompt": f"{_PROMPT_ID_PREFIX}{criterion_id}"},)


def full_mode_request_criteria(
    ticket_id: str, *, ticket_repo_root: str
) -> tuple[dict[str, Any], ...]:
    """The per-ticket INCLUDED criteria set for full mode, as a ``req.criteria`` tuple.
    Production's ``req.criteria`` is already the interpreter's ``route_criteria``
    applies()/overlay-filtered included set (``production_batch_runner`` D5), never the
    raw full registry -- an unfiltered set silently over-requests criteria that do not
    apply to this ticket's level/type/scope, corrupting the finding-set comparison.
    ``route_criteria`` needs a real :class:`PlanContext`, so this assembles one from the
    real ticket store (mirroring the reconstruction ``ProductionBatchRunner.run`` does
    internally) purely to compute the included set BEFORE the batch call. Candidate
    PROMPT/THRESHOLD overrides for these ids still take effect via the installed
    ``.rebar/`` override in the pinned config root; a candidate that adds a wholly new
    id is out of scope (see module docstring)."""
    from rebar.llm.plan_review.context_assembly import assemble_context
    from rebar.llm.plan_review.orchestrator import route_criteria

    ctx = assemble_context(ticket_id, repo_root=ticket_repo_root)
    single, agent = route_criteria(ctx)
    return tuple({"prompt": f"{_PROMPT_ID_PREFIX}{c['id']}"} for c in (*single, *agent))


# ── candidate override installation (mirrors Tier-1's VerifierCandidate exactly) ────
def install_candidate_override(candidate_dir: str | None, config_root: str) -> None:
    """Copy ``candidate_dir``'s ``.rebar/`` subtree into ``config_root/.rebar`` before a
    run, so ``prompts.get_prompt``/``registry.by_id(repo_root=config_root)`` pick up the
    candidate's prompt/routing overrides ahead of the packaged production ones.
    ``candidate_dir=None`` leaves the config root untouched (the ``"current"``
    reproduction run, production's shipped registry)."""
    if candidate_dir is None:
        return
    src = Path(candidate_dir) / ".rebar"
    if not src.is_dir():
        raise FileNotFoundError(f"tier2 candidate dir has no .rebar/ subtree: {candidate_dir!r}")
    dst = Path(config_root) / ".rebar"
    shutil.copytree(src, dst, dirs_exist_ok=True)


# ── the candidate Pass-1 runner (live LLM seam) ─────────────────────────────────────
def run_candidate_pass1(
    ticket_id: str,
    *,
    ticket_repo_root: str,
    criteria: tuple[dict[str, Any], ...],
    frontier_model_id: str,
    config_root: str,
    run_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run ``ProductionBatchRunner`` over ``criteria`` for one sampled review, pinned
    to the frontier model via ``config_root`` (parity's fallback-free ephemeral
    config -- carries any installed candidate override). ``ticket_repo_root`` is the
    SEPARATE real tracker root ``assemble_context`` needs to reconstruct the
    ``PlanContext`` -- distinct from ``config_root``, which governs only
    model-config/prompt/registry resolution (``cfg.repo_path``), exactly as Tier-1
    keeps the verifier's config root separate from the ticket store."""
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import get_runner

    cfg = LLMConfig(model=frontier_model_id, repo_path=config_root, temperature=0.0)
    runner_obj = get_runner(cfg)
    req = BatchRunRequest(
        finder="plan-review-finder",
        criteria=criteria,
        usd_budget=None,
        model_ladder=(frontier_model_id,),
        workflow={},
        target_ticket=ticket_id,
        repo_root=ticket_repo_root,
        run_id=run_id,
        step_id=_TIER,
    )
    result = ProductionBatchRunner(runner=runner_obj).run(req)
    return list(result.outputs.get("findings", [])), dict(result.outputs.get("_usage", {}))


# ── candidate finding-set comparison (Jaccard + gained/lost per criterion) ──────────
_DET_CRITERIA = frozenset(CANONICAL_DET)


def _exclude_det_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop stored findings whose criteria are ENTIRELY DET-floor tier
    (``registry.CANONICAL_DET`` -- P1..P11). DET-floor findings are produced by
    deterministic Python checks (``det_floor``/``det_advisory``/``det_clarity``/
    ``det_lint``), never by the LLM Pass-1 finder ``run_candidate_pass1`` replays,
    so leaving them in the stored-vs-candidate comparison mechanically guarantees a
    "lost"/"relieved" mismatch for every such finding regardless of whether the
    candidate is behaving correctly -- polluting the Jaccard/verdict-flip signature
    with zero-signal noise. A finding with no recorded criteria is left in place
    (nothing to classify it as DET-only)."""
    return [f for f in findings if not (f.get("criteria") and set(f["criteria"]) <= _DET_CRITERIA)]


def _findings_by_criterion(findings: list[dict[str, Any]]) -> dict[str, set[str]]:
    """``{criterion_id: {norm_id, ...}}`` -- a finding tagged with multiple criteria
    contributes its ``norm_id`` to EACH of them."""
    out: dict[str, set[str]] = {}
    for f in findings:
        nid = norm_id(f)
        for cid in f.get("criteria") or []:
            out.setdefault(str(cid), set()).add(nid)
    return out


def finding_set_comparison(
    stored_findings: list[dict[str, Any]], candidate_findings: list[dict[str, Any]]
) -> dict[str, Any]:
    """Whole-set Jaccard plus per-criterion gained/lost, keyed by ``norm_id``.

    Stored findings tagged ENTIRELY with DET-floor criteria (P1..P11) are excluded
    before comparison -- see ``_exclude_det_findings``. Candidate findings never
    include a DET-floor finding (Pass-1 is the only thing replayed), so counting
    them would mechanically inflate "lost" and depress the Jaccard score for
    reasons unrelated to candidate quality."""
    stored_findings = _exclude_det_findings(stored_findings)
    stored_ids = {norm_id(f) for f in stored_findings}
    candidate_ids = {norm_id(f) for f in candidate_findings}
    union = stored_ids | candidate_ids
    jaccard = len(stored_ids & candidate_ids) / len(union) if union else 1.0

    stored_by_crit = _findings_by_criterion(stored_findings)
    candidate_by_crit = _findings_by_criterion(candidate_findings)
    per_criterion: dict[str, Any] = {}
    for cid in set(stored_by_crit) | set(candidate_by_crit):
        s = stored_by_crit.get(cid, set())
        c = candidate_by_crit.get(cid, set())
        per_criterion[cid] = {"gained": len(c - s), "lost": len(s - c), "unchanged": len(s & c)}

    return {"jaccard": jaccard, "per_criterion": per_criterion}


# ── candidate verdict (verify fresh, decide, compare to stored) ────────────────────
def candidate_verdict_flip(
    row: dict[str, Any],
    candidate_findings: list[dict[str, Any]],
    *,
    run_chunk: Any,
    model_id: str,
) -> dict[str, Any]:
    """Verify ``candidate_findings`` fresh via the SAME production seam Tier-1 uses,
    compute their verdict via ``tier0.live_baseline_decisions`` (``orchestrator
    .pass3_over_findings`` called AS-IS), and compare each to the row's
    ALREADY-STORED verdict (index-aligned on the STORED findings -- a candidate
    finding has no stored counterpart to align against, so this counts newly-blocking
    /newly-cleared CRITERIA COVERAGE, not a per-finding pairing). Stored findings
    tagged ENTIRELY with DET-floor criteria (P1..P11) are excluded before computing
    ``stored_blocking_criteria`` -- the candidate replay is Pass-1-only and can
    never surface a DET-floor "block", so leaving them in would count every
    DET-floor blocking finding as permanently "newly relieved" regardless of
    candidate quality (see ``_exclude_det_findings``)."""
    data = row["sidecar_data"]
    stored_findings = _exclude_det_findings(data["findings"])
    execution_review = execution_review_for(data)

    if not candidate_findings:
        return {"stored_blocking_criteria": set(), "candidate_blocking_criteria": set()}

    verify_result = verify_findings(
        candidate_findings,
        context=row.get("description") or "",
        run_chunk=run_chunk,
        window_tokens=sizing.largest_window_tokens(model_id),
        est_tokens=det_floor.est_tokens,
    )
    verifs = verify_result["verifications"]
    candidate_decisions = live_baseline_decisions(
        candidate_findings, verifs, execution_review=execution_review
    )

    stored_blocking = {
        cid
        for f in stored_findings
        if f.get("decision") == "block"
        for cid in (f.get("criteria") or [])
    }
    candidate_blocking = {
        cid
        for f in candidate_decisions
        if f.get("decision") == "block"
        for cid in (f.get("criteria") or [])
    }
    return {
        "stored_blocking_criteria": stored_blocking,
        "candidate_blocking_criteria": candidate_blocking,
    }


def verdict_flip_matrix(per_row_flips: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate ``candidate_verdict_flip`` results: criteria newly blocking under the
    candidate that were NOT blocking in the stored verdict, and criteria relieved
    (the reverse), summed across the sample."""
    newly_blocking = 0
    relieved = 0
    for flip in per_row_flips:
        stored = flip["stored_blocking_criteria"]
        candidate = flip["candidate_blocking_criteria"]
        newly_blocking += len(candidate - stored)
        relieved += len(stored - candidate)
    return {"newly_blocking": newly_blocking, "relieved": relieved}


# ── budget / cost tier ──────────────────────────────────────────────────────────────
def cost_tier_for_criterion(desc: dict[str, Any]) -> str:
    """``"1-TURN"``/``"2-STEP"`` (the ~$0.17-0.25 AC ceiling applies) or ``"AGENT"``
    (real production rate, including the G3/G4 container subset -- no fixed ceiling)."""
    from rebar.llm.plan_review import registry

    tier = registry.exec_tier(desc)
    return "AGENT" if tier == "AGENT" or desc.get("id") in CONTAINER_CRITERIA else tier


# ── the run drivers ──────────────────────────────────────────────────────────────────
def run_tier2_full(
    store_roots: dict[str, str],
    *,
    cache_dir: Path | str,
    sample_path: str | Path,
    candidate_dir: str | None,
    candidate_name: str,
    ticket_repo_root: str,
    ledger_path: str = ledger.DEFAULT_LEDGER_PATH,
) -> dict[str, Any]:
    """Full mode: replay the WHOLE live registry's criteria over the fixed sample
    under ``candidate_dir``'s overrides (``None`` = the reproduction run)."""
    pinned = parity.resolve_pinned_model("pass1")
    pool = build_sampling_pool(store_roots, cache_dir=cache_dir)
    entries = load_sample_file(sample_path)
    sample = resolve_sample_against_pool(entries, pool)
    if not sample:
        raise ValueError(
            f"tier2: fixed sample at {sample_path} resolved to zero rows against the pool"
        )

    estimate_usd = ledger.estimate(_TIER, len(sample))
    ledger.reserve(estimate_usd, ledger_path=ledger_path)

    install_candidate_override(candidate_dir, pinned.config_root)
    from rebar.llm.config import LLMConfig
    from rebar.llm.plan_review import passes
    from rebar.llm.prompting import prompts
    from rebar.llm.runner import RunRequest, get_runner

    cfg = LLMConfig(model=pinned.model_id, repo_path=pinned.config_root, temperature=0.0)
    verify_runner = get_runner(cfg)
    verify_prompt = prompts.get_prompt(passes.PASS_VERIFIER, repo_root=cfg.repo_path)
    usage_rows: list[dict[str, Any]] = []

    def verify_run_chunk(instructions: str, context: str) -> list[dict]:
        system, _meta = prompts.resolve_prompt(
            verify_prompt,
            {"shared_prefix": prompts.shared_plan_prefix(context)},
            repo_root=cfg.repo_path,
        )
        req = RunRequest.for_structured(
            system_prompt=prompts.strip_volatile_marker(system),
            instructions=instructions,
            config=cfg,
            reviewers=["plan-reviewer"],
            output_schema="plan_review_verification",
            bounds=RunRequest.INHERIT_POLICY,
        )
        result = verify_runner.run(req)
        usage = result.get("_usage") or {}
        usage_rows.append(
            {
                "model": pinned.model_id,
                "provider": "bedrock",
                **{f: int(usage.get(f, 0) or 0) for f in _TOKEN_FIELDS},
            }
        )
        return result.get("verifications", []) or []

    from rebar.config import tracker_dir

    tracker_path = str(tracker_dir(ticket_repo_root))
    comparisons: list[dict[str, Any]] = []
    flips: list[dict[str, Any]] = []
    for row in sample:
        run_id = f"{_TIER}-full-{uuid.uuid4().hex[:8]}"
        scratch_root, scratch_ticket_id = materialize_reconstructed_ticket(
            row, tracker_path, ticket_repo_root=ticket_repo_root
        )
        try:
            criteria = full_mode_request_criteria(scratch_ticket_id, ticket_repo_root=scratch_root)
            candidate_findings, p1_usage = run_candidate_pass1(
                scratch_ticket_id,
                ticket_repo_root=scratch_root,
                criteria=criteria,
                frontier_model_id=pinned.model_id,
                config_root=pinned.config_root,
                run_id=run_id,
            )
        finally:
            cleanup_reconstructed_ticket(scratch_root, ticket_repo_root=ticket_repo_root)
        for rec in p1_usage.get("per_call", []):
            usage_rows.append(
                {
                    "model": pinned.model_id,
                    "provider": "bedrock",
                    **{f: int(rec.get(f, 0) or 0) for f in _TOKEN_FIELDS},
                }
            )
        comparisons.append(
            finding_set_comparison(row["sidecar_data"]["findings"], candidate_findings)
        )
        flips.append(
            candidate_verdict_flip(
                row, candidate_findings, run_chunk=verify_run_chunk, model_id=pinned.model_id
            )
        )

    run_id = f"{_TIER}-{candidate_name.replace('/', '_')}-{uuid.uuid4().hex[:12]}"
    ledger_entry = ledger.finalize(
        run_id,
        _TIER,
        candidate_name,
        len(sample),
        {"pass1": pinned.model_id},
        usage_rows,
        ledger_path=ledger_path,
    )

    return {
        "run_id": run_id,
        "mode": "full",
        "candidate": candidate_name,
        "model_id": pinned.model_id,
        "sample_n": len(sample),
        "jaccard_mean": sum(c["jaccard"] for c in comparisons) / len(comparisons)
        if comparisons
        else None,
        "verdict_flip": verdict_flip_matrix(flips),
        "ledger_entry": ledger_entry,
    }


def run_tier2_single_criterion(
    store_roots: dict[str, str],
    *,
    cache_dir: Path | str,
    sample_path: str | Path,
    criterion_id: str,
    ticket_repo_root: str,
    ledger_path: str = ledger.DEFAULT_LEDGER_PATH,
) -> dict[str, Any]:
    """Single-criterion mode: replay ONLY ``criterion_id`` (real single/agent dispatch
    via ``ProductionBatchRunner``) over the fixed sample."""
    from rebar.llm.plan_review import registry

    pinned = parity.resolve_pinned_model("pass1")
    pool = build_sampling_pool(store_roots, cache_dir=cache_dir)
    entries = load_sample_file(sample_path)
    sample = resolve_sample_against_pool(entries, pool)
    if not sample:
        raise ValueError(
            f"tier2: fixed sample at {sample_path} resolved to zero rows against the pool"
        )

    by_id = registry.by_id()
    desc = by_id.get(criterion_id)
    if desc is None:
        raise ValueError(
            f"tier2: unknown criterion id {criterion_id!r} (not in the live registry) -- "
            "ProductionBatchRunner._resolve_criteria would silently drop it into `skipped` "
            "rather than dispatch it, so this fails fast instead of billing a no-op run"
        )
    tier = cost_tier_for_criterion(desc)
    if tier in ("1-TURN", "2-STEP"):
        estimate_usd = 0.25 * len(sample)
    else:
        estimate_usd = ledger.estimate(
            _TIER, len(sample)
        )  # real-rate AGENT criterion; report separately
    ledger.reserve(estimate_usd, ledger_path=ledger_path)

    from rebar.config import tracker_dir

    tracker_path = str(tracker_dir(ticket_repo_root))
    criteria = single_criterion_request_criteria(criterion_id)
    comparisons: list[dict[str, Any]] = []
    usage_rows: list[dict[str, Any]] = []
    for row in sample:
        run_id = f"{_TIER}-{criterion_id}-{uuid.uuid4().hex[:8]}"
        scratch_root, scratch_ticket_id = materialize_reconstructed_ticket(
            row, tracker_path, ticket_repo_root=ticket_repo_root
        )
        try:
            candidate_findings, p1_usage = run_candidate_pass1(
                scratch_ticket_id,
                ticket_repo_root=scratch_root,
                criteria=criteria,
                frontier_model_id=pinned.model_id,
                config_root=pinned.config_root,
                run_id=run_id,
            )
        finally:
            cleanup_reconstructed_ticket(scratch_root, ticket_repo_root=ticket_repo_root)
        for rec in p1_usage.get("per_call", []):
            usage_rows.append(
                {
                    "model": pinned.model_id,
                    "provider": "bedrock",
                    **{f: int(rec.get(f, 0) or 0) for f in _TOKEN_FIELDS},
                }
            )
        stored = [
            f for f in row["sidecar_data"]["findings"] if criterion_id in (f.get("criteria") or [])
        ]
        comparisons.append(finding_set_comparison(stored, candidate_findings))

    run_id = f"{_TIER}-{criterion_id}-{uuid.uuid4().hex[:12]}"
    ledger_entry = ledger.finalize(
        run_id,
        _TIER,
        criterion_id,
        len(sample),
        {"pass1": pinned.model_id},
        usage_rows,
        ledger_path=ledger_path,
    )

    return {
        "run_id": run_id,
        "mode": "single-criterion",
        "criterion_id": criterion_id,
        "exec_tier": tier,
        "model_id": pinned.model_id,
        "sample_n": len(sample),
        "jaccard_mean": sum(c["jaccard"] for c in comparisons) / len(comparisons)
        if comparisons
        else None,
        "ledger_entry": ledger_entry,
    }


def render_tier2_report(result: dict[str, Any]) -> str:
    lines = [
        f"# Tier-2 Pass-1 replay ({result['mode']})",
        "",
        f"Run id: `{result['run_id']}`",
        f"Model: `{result['model_id']}`",
        f"Sample: {result['sample_n']}",
    ]
    if result["mode"] == "single-criterion":
        lines.append(f"Criterion: `{result['criterion_id']}` (exec_tier={result['exec_tier']})")
    else:
        lines.append(f"Candidate: `{result['candidate']}`")
    lines += [
        f"Ledger cost: ${result['ledger_entry']['usd']:.2f}",
        f"Mean Jaccard: {result['jaccard_mean']}",
    ]
    if "verdict_flip" in result:
        vf = result["verdict_flip"]
        lines.append(
            f"Verdict flip: newly_blocking={vf['newly_blocking']} relieved={vf['relieved']}"
        )
    return "\n".join(lines) + "\n"
