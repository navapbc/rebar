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
from typing import Any

from rebar.llm.config import LLMConfig
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


def judge_one(first: dict, second: dict, cfg: LLMConfig, runner: Runner | None) -> dict:
    """One ORDERED-pair judge call ("first <relation> second"). Returns the overlap_verdict
    dict; on ANY error (timeout / malformed output) → treated as abstain (dropped), logged,
    never raised to the caller."""
    try:
        prompt = prompts.get_prompt("overlap-judge", repo_root=cfg.repo_path)
        system_prompt, _meta = prompts.resolve_prompt(prompt, {}, repo_root=cfg.repo_path)
        instructions = f"FIRST:\n{_digest_block(first)}\n\nSECOND:\n{_digest_block(second)}"
        req = RunRequest(
            system_prompt=system_prompt,
            instructions=instructions,
            config=cfg,
            reviewers=["overlap-judge"],
            mode="structured",
            output_schema="overlap_verdict",
            execution_mode="single_turn",
        )
        res = get_runner(cfg, override=runner).run(req)
        return {
            "relation": res.get("relation", "related_distinct"),
            "shared_artifact": res.get("shared_artifact"),
            "confidence": float(res.get("confidence", 0.0)),
            "abstain": bool(res.get("abstain", False)),
        }
    except Exception:  # noqa: BLE001 — a judge failure is an abstain, never blocks the caller
        logger.warning("overlap judge call failed; treating as abstain", exc_info=True)
        return dict(_ABSTAIN)


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
    findings: list[dict] = []
    for cand in candidates:
        cand_id = getattr(cand, "ticket_id", cand)
        cand_digest = corpus.get(cand_id)
        if not isinstance(cand_digest, dict):
            continue
        r1 = judge_one(query_digest, cand_digest, cfg, runner)
        r2 = judge_one(cand_digest, query_digest, cfg, runner)
        finding = aggregate(query_id, cand_id, r1, r2, cfg)
        if finding is not None:
            findings.append(finding)
    findings.sort(key=lambda f: -f["confidence"])
    return findings[: cfg.overlap_surface_cap]
