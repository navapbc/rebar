"""The Cupid ticket-digest enrichment op (epic only-crave-art, story ee3d).

A structured single-turn LLM op that extracts a compact, normalized dedup digest
``{problem_keywords, component_or_area, key_entities, propositions}`` from ONE ticket, for
store-wide overlap detection WITHOUT embeddings (the Cupid pattern, arXiv:2308.10022).

Modeled on the structured single-turn pattern in :mod:`rebar.llm.plan_review.passes`
(``RunRequest(mode="structured", execution_mode="single_turn")``), NOT on the agentic
review/verify ops. It reads the ticket via the ``rebar.llm`` facade ``_reads.show_ticket``
(never importing ``_engine_support.gates``) and assembles the source text inline.
"""

from __future__ import annotations

from rebar.llm.config import LLMConfig
from rebar.llm.prompting import prompts
from rebar.llm.runner import Runner, RunRequest, get_runner

# The four ticket_digest fields (ticket_digest.schema.json). The runner's structured
# return additionally carries runner/model/trace_id provenance keys, which the op drops.
_DIGEST_FIELDS = ("problem_keywords", "component_or_area", "key_entities", "propositions")


def _assemble_text(state: dict) -> str:
    """title + description + comment bodies, ``"\\n"``-joined (empties skipped).

    A trivial INLINE join — deliberately NOT an import of the module-private
    ``rebar._engine_support.gates._ticket_text``: no ``rebar.llm.*`` module imports
    ``_engine_support.gates`` (that would be a layering violation), and this text only
    feeds the LLM prompt, so it needs no shared normalizer.
    """
    parts: list[str] = []
    if state.get("title"):
        parts.append(str(state["title"]))
    if state.get("description"):
        parts.append(str(state["description"]))
    for c in state.get("comments", []) or []:
        body = (c or {}).get("body", "")
        if body:
            parts.append(str(body))
    return "\n".join(parts)


def enrich(
    ticket_id: str | None = None,
    *,
    text: str | None = None,
    repo_root=None,
    config: LLMConfig | None = None,
    runner: Runner | None = None,
) -> dict:
    """Extract a ``ticket_digest`` from a ticket (by ``ticket_id``) or from raw ``text``.

    Exactly one of ``ticket_id`` / ``text`` must be given (``text`` bypasses the store
    read — the injection seam offline tests use). Returns
    ``{"digest": {<the four ticket_digest fields>}, "low_proposition_count": bool}``; the
    ``digest`` validates against ``ticket_digest.schema.json``.

    Errors propagate with no partial write: a shape-invalid structured payload raises
    ``FindingsError`` and an absent structured response raises ``StructuredOutputError``
    (both from the runner's ``finalize_outcome``); an absent LLM raises
    ``LLMUnavailableError`` / ``LLMConfigError``.
    """
    if (ticket_id is None) == (text is None):
        raise ValueError("enrich() requires exactly one of ticket_id or text")

    cfg = config or LLMConfig.from_env(repo_root=repo_root)

    if text is not None:
        source = text
    else:
        from rebar import _reads

        assert ticket_id is not None  # guaranteed by the exactly-one check above
        state = _reads.show_ticket(ticket_id, repo_root=repo_root)
        source = _assemble_text(state)

    prompt = prompts.get_prompt("ticket-digest", repo_root=cfg.repo_path)
    system_prompt, _meta = prompts.resolve_prompt(prompt, {}, repo_root=cfg.repo_path)

    req = RunRequest(
        system_prompt=system_prompt,
        instructions=source,
        config=cfg,
        reviewers=["ticket-digest"],
        mode="structured",
        output_schema="ticket_digest",
        execution_mode="single_turn",
    )
    run_result = get_runner(cfg, override=runner).run(req)

    # The structured return is {<four digest fields>, runner, model, trace_id}; select just
    # the digest fields (validation already ran in the runner, so all four are present).
    digest = {k: run_result[k] for k in _DIGEST_FIELDS}

    # Config-bound proposition count: truncate above max; flag (never raise) below min.
    props = list(digest.get("propositions") or [])
    low_proposition_count = False
    if len(props) > cfg.overlap_propositions_max:
        props = props[: cfg.overlap_propositions_max]
    elif len(props) < cfg.overlap_propositions_min:
        low_proposition_count = True
    digest["propositions"] = props

    return {"digest": digest, "low_proposition_count": low_proposition_count}
