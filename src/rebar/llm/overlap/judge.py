"""Stage-2 bounded LLM pairwise overlap judge (epic only-crave-art, story 9022).

Precision lives here, not in a similarity threshold: a bounded LLM pairwise classifier over
the ~K Stage-1 candidates. Each candidate is judged in BOTH orderings (position-bias
mitigation); a relation is surfaced only when both orderings agree, each cites a concrete
shared artifact, and both clear the confidence threshold. Directional relations
(supersedes/depends_on) carry a canonical direction so the emitted `rebar link` command is
never inverted. Surfaces at most `overlap_surface_cap` advisory findings; NEVER auto-links.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from rebar.llm.config import LLMConfig
from rebar.llm.model_classes import STANDARD_CLASS, resolve_model_string
from rebar.llm.prompting import prompts
from rebar.llm.runner import Runner, RunRequest, get_runner

logger = logging.getLogger(__name__)

_DIRECTIONAL = {"supersedes", "depends_on"}
_ABSTAIN = {"relation": "unrelated", "shared_artifact": None, "confidence": 0.0, "abstain": True}


def _digest_block(digest: dict) -> str:
    def _join(v: Any) -> str:
        return ", ".join(str(x) for x in v) if isinstance(v, list) else str(v or "")

    return (
        f"component_or_area: {_join(digest.get('component_or_area'))}\n"
        f"problem_keywords: {_join(digest.get('problem_keywords'))}\n"
        f"key_entities: {_join(digest.get('key_entities'))}\n"
        f"propositions: {_join(digest.get('propositions'))}"
    )


# The text substituted into the FIRST/SECOND slot whose digest has been hoisted into the system
# prompt, and the orientation that introduces it there. The base prompt tells the model it is
# "given TWO ticket digests in a fixed order"; the header below reconciles that framing with the
# split, so `reviewers/overlap_judge.md` keeps describing the default (non-hoisted) shape.
_SHARED_DIGEST_REF = "the QUERY TICKET DIGEST given in your system prompt"
_SHARED_DIGEST_HEADER = (
    "QUERY TICKET DIGEST — one of the two digests named below is given HERE rather than in the\n"
    "user message, because it is the same ticket in every pair of this batch. The user message\n"
    "names whether it is FIRST or SECOND for the pair at hand; read it in that position."
)


def _hoisted_system_prompt(base: str, shared: dict) -> str:
    return f"{base}\n\n{_SHARED_DIGEST_HEADER}\n\n{_digest_block(shared)}"


def _pair_instructions(first: dict, second: dict, shared_side: str | None) -> str:
    first_text = _SHARED_DIGEST_REF if shared_side == "first" else _digest_block(first)
    second_text = _SHARED_DIGEST_REF if shared_side == "second" else _digest_block(second)
    return f"FIRST:\n{first_text}\n\nSECOND:\n{second_text}"


# How many candidate digests ride in ONE judge call. Bounded rather than "all of them": the
# Stage-1 candidate set is capped at ``overlap_k`` (20), and a single prompt listing twenty
# digests asks the model to hold twenty independent judgements at once, which is the
# cross-contamination risk the prompt's per-candidate independence rules exist to contain.
_CANDIDATES_PER_CALL = 6


def _candidate_listing(pairs: list[tuple[str, dict]]) -> str:
    """The labelled candidate digests. The label is the key the response is matched back on, so
    it is emitted verbatim and read back verbatim — never positionally."""
    return "\n\n".join(f"[candidate_id: {cid}]\n{_digest_block(digest)}" for cid, digest in pairs)


def _batch_instructions(pairs: list[tuple[str, dict]], *, shared_side: str) -> str:
    listing = _candidate_listing(pairs)
    if shared_side == "first":
        return f"FIRST:\n{_SHARED_DIGEST_REF}\n\nSECOND CANDIDATES:\n{listing}"
    return f"FIRST CANDIDATES:\n{listing}\n\nSECOND:\n{_SHARED_DIGEST_REF}"


def _verdict(payload: dict) -> dict:
    """The four overlap_verdict fields, defaulted so a sparse payload is still readable."""
    return {
        "relation": payload.get("relation", "related_distinct"),
        "shared_artifact": payload.get("shared_artifact"),
        "confidence": float(payload.get("confidence", 0.0)),
        "abstain": bool(payload.get("abstain", False)),
    }


def judge_one(
    first: dict,
    second: dict,
    cfg: LLMConfig,
    runner: Runner | None,
    *,
    shared_side: str | None = None,
) -> dict:
    """One ORDERED-pair judge call ("first <relation> second"). Returns the overlap_verdict
    dict; on ANY error (timeout / malformed output) → treated as abstain (dropped), logged,
    never raised to the caller.

    ``shared_side`` names the side of the pair — ``"first"`` or ``"second"`` — whose digest is
    IDENTICAL across every call of the batch, so it can be carried in the system prompt (the
    block the runner puts a cache breakpoint on) instead of being re-sent in each volatile user
    message. The vacated slot keeps its FIRST/SECOND label and holds a reference to the hoisted
    block, so the prompt's directional "FIRST <relation> SECOND" reading is unchanged. ``None``
    (the default) reproduces the un-hoisted request bytes exactly.
    """
    try:
        # MODEL CLASS: `standard`. Chosen on the prompt's shape, not on "it's a sub-call": the
        # overlap-judge prompt is `execution_mode: single_turn`, tool-less and "Not a reviewer",
        # but it must pick ONE relation from a closed 5-value set AND cite a named shared artifact
        # while "false flags are costly" — precision-sensitive JUDGEMENT, which is what the
        # operator's rule puts on the sonnet-equivalent `standard` class. NOT `frontier`: this is
        # the volume site (18 of the 23 LLM calls in one measured plan review), so frontier would
        # be the most expensive possible reading of the fix.
        #
        # Bound HERE, per call, rather than inherited from `cfg`: passing raw `cfg` made every
        # judge call run `cfg.model` and ignore the configured class table entirely, so an operator
        # who cut over by configuring the three classes left the MAJORITY of plan-review traffic on
        # their old provider (bug afeb). Inside the try, so a config error is an abstain like any
        # other judge failure — the overlap step stays advisory and never blocks a review.
        cfg = replace(cfg, model=resolve_model_string(STANDARD_CLASS))
        prompt = prompts.get_prompt("overlap-judge", repo_root=cfg.repo_path)
        system_prompt, _meta = prompts.resolve_prompt(prompt, {}, repo_root=cfg.repo_path)
        if shared_side is not None:
            system_prompt = _hoisted_system_prompt(
                system_prompt, first if shared_side == "first" else second
            )
        instructions = _pair_instructions(first, second, shared_side)
        req = RunRequest(
            system_prompt=system_prompt,
            instructions=instructions,
            config=cfg,
            reviewers=["overlap-judge"],
            mode="structured",
            output_schema="overlap_verdict",
            execution_mode="single_turn",
        )
        return _verdict(get_runner(cfg, override=runner).run(req))
    except Exception:
        logger.warning("overlap judge call failed; treating as abstain", exc_info=True)
        return dict(_ABSTAIN)


def judge_batch(
    shared: dict,
    pairs: list[tuple[str, dict]],
    cfg: LLMConfig,
    runner: Runner | None,
    *,
    shared_side: str,
) -> dict[str, dict]:
    """ONE judge call carrying ``shared`` against every candidate in ``pairs``, returning
    ``{candidate_id: verdict}``.

    Every id in ``pairs`` is present in the result: a candidate the model did not answer for —
    or answered unreadably — gets an abstain, so a partial batch degrades per candidate instead
    of taking its neighbours down with it. On ANY error the whole batch abstains, logged, never
    raised: the overlap step is advisory and must not fail a review.

    ``shared_side`` names which side of the ordered pair the shared digest occupies, exactly as
    on :func:`judge_one`; the other side becomes the labelled candidate list.
    """
    verdicts = {cid: dict(_ABSTAIN) for cid, _ in pairs}
    try:
        # Same per-call class binding as judge_one, and for the same reason (bug afeb).
        cfg = replace(cfg, model=resolve_model_string(STANDARD_CLASS))
        prompt = prompts.get_prompt("overlap-judge", repo_root=cfg.repo_path)
        system_prompt, _meta = prompts.resolve_prompt(prompt, {}, repo_root=cfg.repo_path)
        req = RunRequest(
            system_prompt=_hoisted_system_prompt(system_prompt, shared),
            instructions=_batch_instructions(pairs, shared_side=shared_side),
            config=cfg,
            reviewers=["overlap-judge"],
            mode="structured",
            output_schema="overlap_verdict_batch",
            execution_mode="single_turn",
        )
        res = get_runner(cfg, override=runner).run(req)
        entries = res.get("verdicts") if isinstance(res, dict) else None
        if not isinstance(entries, list):
            return verdicts
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            cid = entry.get("candidate_id")
            # Matched on the LABEL, never on position: a reordered response must not attribute
            # one candidate's relation to another. An id we did not ask about, and a second
            # answer for one already read, are both dropped rather than allowed to revise a
            # verdict — one question, one answer.
            if cid not in verdicts or cid in seen:
                continue
            seen.add(cid)
            verdicts[cid] = _verdict(entry)
    except Exception:
        logger.warning("overlap judge batch call failed; treating as abstain", exc_info=True)
    return verdicts


def _finding(
    src: str, tgt: str, relation: str, artifact: str | None, conf: float, cand: str
) -> dict:
    return {
        "source_id": src,
        "target_id": tgt,
        "counterpart_id": cand,
        "relation": relation,
        "shared_artifact": artifact,
        "confidence": conf,
        "link_command": f"rebar link {src} {tgt} {relation}",
    }


def aggregate(query_id: str, cand_id: str, r1: dict, r2: dict, cfg: LLMConfig) -> dict | None:
    """Aggregate the two orderings into a surfaced finding, or None (fail-safe toward
    "distinct"). ``r1`` = "query r1 cand", ``r2`` = "cand r2 query"."""
    # Abstain in EITHER ordering → downgrade (no surface, no slot consumed).
    if r1.get("abstain") or r2.get("abstain"):
        return None
    # Both must cite a concrete shared artifact …
    if not (r1.get("shared_artifact") and r2.get("shared_artifact")):
        return None
    # … and both must clear the confidence threshold.
    if not (
        r1.get("confidence", 0.0) >= cfg.overlap_conf_threshold
        and r2.get("confidence", 0.0) >= cfg.overlap_conf_threshold
    ):
        return None
    rel1, rel2 = r1.get("relation"), r2.get("relation")
    conf = min(r1["confidence"], r2["confidence"])

    # duplicates (symmetric): both orderings agree → canonical sorted-id pair.
    if rel1 == "duplicates" and rel2 == "duplicates":
        src, tgt = sorted([query_id, cand_id])
        return _finding(src, tgt, "duplicates", r1["shared_artifact"], conf, cand_id)

    # Directional (supersedes/depends_on): the asserting ordering sets source→target. If BOTH
    # orderings assert the SAME directional label with opposite subjects (a contradiction),
    # downgrade to related_distinct (not surfaced).
    if rel1 in _DIRECTIONAL:
        if rel2 == rel1:  # "query sup cand" AND "cand sup query" → self-contradiction
            return None
        return _finding(query_id, cand_id, rel1, r1["shared_artifact"], conf, cand_id)
    if rel2 in _DIRECTIONAL:
        return _finding(cand_id, query_id, rel2, r2["shared_artifact"], conf, cand_id)

    return None


def judge(
    query_id: str,
    query_digest: dict,
    candidates: list,
    corpus: dict[str, dict],
    *,
    config: LLMConfig | None = None,
    runner: Runner | None = None,
) -> list[dict]:
    """Judge each candidate in both orderings and return up to ``overlap_surface_cap``
    highest-confidence advisory findings (each a ready-to-run ``rebar link`` command).
    ``candidates`` may be ``OverlapCandidate``s or bare ticket-id strings."""
    cfg = config or LLMConfig.from_env()
    pairs: list[tuple[str, dict]] = []
    for cand in candidates:
        cand_id = getattr(cand, "ticket_id", cand)
        cand_digest = corpus.get(cand_id)
        if isinstance(cand_digest, dict):  # a candidate we cannot ground is never paid for
            pairs.append((cand_id, cand_digest))

    findings: list[dict] = []
    for start in range(0, len(pairs), _CANDIDATES_PER_CALL):
        batch = pairs[start : start + _CANDIDATES_PER_CALL]
        # Two calls per batch, not two per candidate: each carries the hoisted system block once
        # for the whole batch instead of once per pair. The query digest is the batch-invariant
        # side in BOTH orderings, so the block is byte-identical across them.
        r1 = judge_batch(query_digest, batch, cfg, runner, shared_side="first")
        r2 = judge_batch(query_digest, batch, cfg, runner, shared_side="second")
        for cand_id, _digest in batch:
            finding = aggregate(query_id, cand_id, r1[cand_id], r2[cand_id], cfg)
            if finding is not None:
                findings.append(finding)
    findings.sort(key=lambda f: -f["confidence"])
    return findings[: cfg.overlap_surface_cap]
