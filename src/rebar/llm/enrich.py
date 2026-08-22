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

from dataclasses import replace

from rebar.llm.config import LLMConfig
from rebar.llm.model_classes import TRIVIAL_CLASS, resolve_model_string
from rebar.llm.prompting import prompts
from rebar.llm.runner import Runner, RunRequest, get_runner

# The four ticket_digest fields (ticket_digest.schema.json). The runner's structured
# return additionally carries runner/model/trace_id provenance keys, which the op drops.
_DIGEST_FIELDS = ("problem_keywords", "component_or_area", "key_entities", "propositions")

# Chars-per-token for the physical context ceiling. VERBATIM the constant and the reasoning of
# `rebar.llm.workflow.completion_criteria._CONTEXT_CHARS_PER_TOKEN`: English prose averages ~4
# chars/token, so 2 is deliberately conservative and leaves room for the tokenizer's worst case
# on the mixed prose/code/log text a ticket body actually contains. Duplicated rather than
# imported because `rebar.llm.enrich` is on the STORE WRITE path (enrich_drain.maybe_drain) and
# must not drag the completion-verifier workflow package into that import graph.
_CONTEXT_CHARS_PER_TOKEN = 2

# The visible shortening marker. Contract: the marker text is part of the string that is SENT,
# so an operator reading the prompt (or a digest that looks thin) can see the source was cut
# rather than silently losing the tail. Wording mirrors
# `completion_prefetch.fit_within_ceiling`'s "<prefetch truncated to fit context ceiling>".
_TRUNCATION_MARKER = "\n... <ticket source truncated to fit the model context ceiling> ..."


def _bound_source(source: str, model: str | None, *, reserved_chars: int = 0) -> str:
    """Shorten *source* so it can never overflow *model*'s own context window.

    Bug 569c-931f-69a2-4c1d (spongy-illjudged-terrier): ``_assemble_text`` concatenates title +
    description + EVERY comment body with no bound at all. On a long-lived ticket that reaches
    millions of characters, and the provider rejects the request outright — ``status_code: 400
    ... 'prompt is too long: 206826 tokens > 200000 maximum'`` against the `trivial` class's
    200k-token window — so the queue's LARGEST entries were the ones that could never drain.

    WHY TRUNCATE HERE, given :mod:`rebar.llm.pai_retry` states that "rebar fails closed rather
    than SHORTENING authoritative system / user / tool content"?
    That rule is scoped, not universal, and this content sits on the other side of the scope
    line. ``pai_retry``'s ``_wire_keeps_response`` governs the *authoritative* conversation
    carried on a RETRY wire for a verification op: the system prompt, the user's actual
    instruction, and prior tool results are the evidence a verdict is computed from, so
    silently shortening them would let a gate pass on partial evidence — an incorrect OUTCOME,
    which is exactly what failing closed prevents.
    The ticket-digest source is not that. It is *lossy summarizer input*: this op extracts four
    constrained normalization fields for dedup candidate generation, and it is already lossy by
    construction — ``enrich()`` truncates its own ``propositions`` output at
    ``cfg.overlap_propositions_max`` a few lines below. A digest computed from a ticket's title,
    description, and first N comments is a slightly weaker dedup hint; a digest that never
    exists at all (the pre-fix behaviour) is no hint AND an infinite re-claim loop. The project
    already takes exactly this position for assistance-class content in
    ``completion_prefetch.fit_within_ceiling``, which trims a prefetch section to this same
    ceiling and appends a visible marker rather than letting the run die. Failing closed here
    would buy no correctness and cost the whole feature on precisely the tickets that most need
    dedup.

    Behaviour (mirrors ``fit_within_ceiling`` + ``comment_limits.truncate_comment_body``):

    * a source at or below the budget is returned **byte-identical**;
    * an over-budget source is cut on the last ``"\\n"`` join boundary that fits — the join
      ``_assemble_text`` itself produces — so whole trailing COMMENTS are dropped rather than a
      comment being severed mid-word, falling back to a hard cut when no boundary fits. Title
      and description are joined first, so they always survive;
    * the marker is counted against the budget and the result is defensively re-clamped, so the
      return NEVER overflows the ceiling even after appending it;
    * the operation is **idempotent** — the returned string is always within the budget, so a
      second application returns it unchanged. This is load-bearing, not cosmetic: the digest
      sidecar keys a stored digest by the ticket's content hash
      (``overlap.digest_sidecar.freshness``), so a non-idempotent bound would make the same
      ticket produce a different prompt on each drain and churn the sidecar forever.

    ``reserved_chars`` is the caller's system-prompt size. The ceiling is already conservative,
    but the budget should not pretend the system prompt is free.
    """
    from rebar.llm.model_classes import own_window_tokens

    ceiling = own_window_tokens(model) * _CONTEXT_CHARS_PER_TOKEN
    # Never let a pathological reservation drive the budget to nothing: floor it at half the
    # ceiling. The reservation is ~1.9k chars against a six-figure ceiling in practice, so this
    # floor is defensive only.
    budget = max(ceiling - max(reserved_chars, 0), ceiling // 2)
    if len(source) <= budget:
        return source

    allowance = budget - len(_TRUNCATION_MARKER)
    if allowance <= 0:  # defensive: an absurdly small window
        return source[:budget]
    boundary = source[:allowance].rfind("\n")
    trimmed = source[:boundary] if boundary > 0 else source[:allowance]
    result = trimmed + _TRUNCATION_MARKER
    return result[:budget] if len(result) > budget else result


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
    # MODEL CLASS: `trivial`. Chosen on the prompt's shape: ticket-digest is `execution_mode:
    # single_turn`, has NO tools, is explicitly "Not a reviewer", and extracts four constrained
    # structured fields from text it is handed — narrow, canonicalizing work with no open-ended
    # reasoning. It is also the highest-VOLUME site of bug afeb's four, because it runs on the
    # ticket STORE WRITE path (enrich_drain.maybe_drain, from _store/event_append and _store/push),
    # so it is the cheapest win of the four.
    #
    # Bound HERE rather than at the callers: `cfg` was previously handed to the RunRequest raw, so
    # the digest ran `cfg.model` and ignored the class table — and because the write path builds its
    # own config from the environment and passes none in, a caller-side fix would have missed
    # exactly the path that matters most. Every route into enrich() inherits this one.
    cfg = replace(cfg, model=resolve_model_string(TRIVIAL_CLASS, cfg.repo_path))

    if text is not None:
        source = text
    else:
        from rebar import _reads

        assert ticket_id is not None  # guaranteed by the exactly-one check above
        state = _reads.show_ticket(ticket_id, repo_root=repo_root)
        source = _assemble_text(state)

    prompt = prompts.get_prompt("ticket-digest", repo_root=cfg.repo_path)
    system_prompt, _meta = prompts.resolve_prompt(prompt, {}, repo_root=cfg.repo_path)

    # Bound the prompt AFTER assembly and against the RESOLVED model (`cfg.model`, set by the
    # model-class resolution above), so BOTH entry paths inherit it — the `text=` injection seam
    # and the `ticket_id=` store read. Deliberately NOT inside `_assemble_text`: that helper is
    # only on the store-read path, so bounding there would leave `text=` unbounded, and it would
    # also have to learn the model, which it has no business knowing. Bug 569c-931f-69a2-4c1d.
    source = _bound_source(source, cfg.model, reserved_chars=len(system_prompt))

    req = RunRequest.for_structured(
        system_prompt=system_prompt,
        instructions=source,
        config=cfg,
        reviewers=["ticket-digest"],
        output_schema="ticket_digest",
        bounds=RunRequest.INHERIT_POLICY,
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
