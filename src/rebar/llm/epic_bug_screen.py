"""The epic-close bug screen: haiku relevance triage over out-of-hierarchy bugs (ticket 4b54).

Agents file bugs OUTSIDE an epic's hierarchy during epic execution and deem them
out-of-scope/pre-existing even when they are defects in the epic's own deliverable; the
direct-children close gate cannot see them (event-precise backtest over 56 epic closes: 2 real
at-close escapes, 30a2 and 5b09). The gate is three-staged, cheapest-first:

1. **DET caused_by floor** (:func:`rebar.llm.completion.epic_bug_floor_findings`) — an
   open/in_progress bug with a ``caused_by`` edge into the subtree deterministically blocks
   the close (enforced in ``gate_ops.completion_precheck``, no LLM call). The hard tier.
2. **DET candidate filter** (:func:`rebar.llm.completion.epic_bug_candidates`) — status/type
   + (created-after-first-claim OR linked-any-relation-any-depth, both directions), ceiling
   :data:`rebar.llm.completion.EPIC_BUG_SCREEN_CEILING`.
3. **This module** — one single-turn TRIVIAL-class (haiku-tier) call per candidate, forced
   choice ``A`` (defect in something this epic changed/built) / ``B`` (same subsystem,
   pre-existing or adjacent) / ``C`` (unrelated) + one-line citation. A-verdicts are forwarded
   to the completion verifier as a compact evidence block (:data:`FORWARD_CAP` rows of
   title + citation + id, ~40 tokens each) appended INSIDE the precheck-assembled fenced
   context; the verifier adjudicates disposition via its native read-only ``show_ticket``
   tool. A false NEGATIVE here is no worse than today (the bug was invisible before); a false
   POSITIVE costs the verifier one adjudication.

The screen NEVER blocks a close by itself: any failure — model down, malformed output, store
read error — degrades OPEN (logged, candidate treated as ``C`` / screen skipped). The tally
(per-bug verdict + citation + unevaluated-overflow count) is recorded on the completion
sidecar for audit and live false-negative calibration.

Model-class + caching discipline: calls bind the TRIVIAL class per call (never raw
``cfg.model`` — bug afeb: hand-built sub-calls bypassed the class table) and put the epic
material in the SYSTEM prompt so the provider's prompt cache amortizes it across the fan-out;
small epics may fall under the provider's cache floor (bug 7a79) — acceptable, the trivial
tier is cheap uncached.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

from rebar.llm.config import LLMConfig
from rebar.llm.model_classes import TRIVIAL_CLASS, resolve_model_string
from rebar.llm.prompting import prompts
from rebar.llm.runner import Runner, RunRequest, get_runner

logger = logging.getLogger(__name__)

# At most this many A-verdict rows are forwarded to the verifier in full (title + citation +
# id); further A-verdicts degrade to a titles-only overflow line so the verifier's context
# window is never squeezed by a pathological close.
FORWARD_CAP = 8
_PROMPT_ID = "epic-bug-screen"
_OUTPUT_SCHEMA = "epic_bug_screen_verdict"
_BLOCK_HEADER = "UNRESOLVED BUG CANDIDATES (epic-close screen)"
_FAN_OUT_WORKERS = 8
# The non-surfacing degrade verdict: any per-candidate failure coerces to C (unrelated), so a
# broken screen can only UNDER-report — it never fabricates an A-candidate or blocks a close.
_DEGRADE = {"verdict": "C", "citation": ""}

ScreenFn = Callable[[dict, str], dict]


def _epic_material(root_state: dict, children_titles: list[str]) -> str:
    """The epic block carried in the SYSTEM prompt (title/description/AC + children titles) —
    identical across the whole fan-out, so the provider's prompt cache amortizes it."""
    lines = [
        "== EPIC UNDER CLOSE ==",
        f"id: {root_state.get('ticket_id', '')}",
        f"title: {root_state.get('title', '')}",
        "",
        str(root_state.get("description") or "(no description)"),
    ]
    if children_titles:
        lines += ["", "== CHILD TICKETS (the epic's delegated work) =="]
        lines += [f"- {t}" for t in children_titles]
    return "\n".join(lines)


def _bug_digest(bug: dict) -> str:
    """The volatile per-call side: one candidate bug, description bounded."""
    description = str(bug.get("description") or "")[:2000]
    return (
        f"== CANDIDATE BUG ==\n"
        f"id: {bug.get('ticket_id', '')}\n"
        f"title: {bug.get('title', '')}\n"
        f"status: {bug.get('status', '')}\n\n"
        f"{description or '(no description)'}"
    )


def _screen_one(bug: dict, system_prompt: str, cfg: LLMConfig, runner: Runner | None) -> dict:
    """One single-turn forced-choice screen call. Mirrors ``overlap.judge.judge_one``: the
    model CLASS is bound here per call (bug afeb — inheriting raw ``cfg.model`` would ignore
    the operator's class table), inside the try so a config error degrades like any other
    screen failure."""
    cfg = replace(cfg, model=resolve_model_string(TRIVIAL_CLASS))
    req = RunRequest(
        system_prompt=system_prompt,
        instructions=(
            f"{_bug_digest(bug)}\n\n"
            "Answer for THIS bug against the epic in your system prompt: verdict A, B, or C "
            "plus a one-line citation."
        ),
        config=cfg,
        reviewers=[_PROMPT_ID],
        mode="structured",
        output_schema=_OUTPUT_SCHEMA,
        execution_mode="single_turn",
    )
    return get_runner(cfg, override=runner).run(req)


def _normalized(raw: Any) -> dict:
    """Coerce a raw screen response through the registered contract's normalizing validator
    (out-of-vocabulary / malformed / missing -> the non-surfacing ``C``)."""
    from rebar.llm.contracts import epic_bug_screen_verdict_response_model

    model = epic_bug_screen_verdict_response_model()
    if not isinstance(raw, dict):
        return dict(_DEGRADE)
    try:
        parsed = model(
            verdict=raw.get("verdict", "C"),
            citation=str(raw.get("citation") or ""),
        )
        return {"verdict": parsed.verdict, "citation": parsed.citation}
    except Exception:  # noqa: BLE001 — a malformed screen output degrades open, never raises
        return dict(_DEGRADE)


def screen_candidates(
    root_state: dict,
    candidates: list[dict],
    cfg: LLMConfig | None,
    runner: Runner | None,
    *,
    screen_fn: ScreenFn | None = None,
    children_titles: list[str] | None = None,
) -> list[dict]:
    """Screen every candidate, warm-then-fan-out; returns the tally (one row per candidate:
    ``{ticket_id, title, verdict, citation}``, order preserved).

    The FIRST candidate runs to completion alone to write the shared system-prompt prefix
    into the provider cache, then the rest fan out concurrently and read the warmed prefix —
    mirroring the plan-review container orchestration (``plan_review/pass1.py``,
    warm-then-fan-out; the helper is not lifted from there because pass1's version is
    entangled with bin-packing and budget records). A per-candidate failure degrades THAT
    candidate to ``C`` — including the warm call: an unwarmed fan-out only forfeits cache
    hits, and the trivial tier is cheap uncached (bug 7a79 note).

    ``screen_fn`` is the LLM-free test seam: ``(bug, system_prompt) -> raw verdict dict``.
    """
    if not candidates:
        return []
    if screen_fn is None:
        if cfg is None:
            cfg = LLMConfig.from_env()
        bound_cfg, bound_runner = cfg, runner
        prompt = prompts.get_prompt(_PROMPT_ID, repo_root=cfg.repo_path)
        prompt_body, _meta = prompts.resolve_prompt(prompt, {}, repo_root=cfg.repo_path)

        def screen_fn(bug: dict, sp: str) -> dict:
            return _screen_one(bug, sp, bound_cfg, bound_runner)

        system_prompt = f"{prompt_body}\n\n{_epic_material(root_state, children_titles or [])}"
    else:
        system_prompt = _epic_material(root_state, children_titles or [])

    def _row(bug: dict) -> dict:
        try:
            verdict = _normalized(screen_fn(bug, system_prompt))
        except Exception:  # noqa: BLE001 — degrade open per candidate; the close never fails here
            logger.warning(
                "epic bug screen call failed for %s; degrading to C (unrelated)",
                bug.get("ticket_id"),
                exc_info=True,
            )
            verdict = dict(_DEGRADE)
        return {
            "ticket_id": bug.get("ticket_id", ""),
            "title": bug.get("title", ""),
            "verdict": verdict["verdict"],
            "citation": verdict["citation"],
        }

    tally = [_row(candidates[0])]  # the warm call: completes before any fan-out starts
    rest = candidates[1:]
    if rest:
        with ThreadPoolExecutor(max_workers=min(_FAN_OUT_WORKERS, len(rest))) as pool:
            tally.extend(pool.map(_row, rest))
    return tally


def candidate_block(tally: list[dict], *, screen_overflow: int) -> str:
    """The compact evidence block forwarded to the completion verifier — A-verdicts only,
    ``FORWARD_CAP`` full rows (title + screen citation + id, ~40 tokens each) then a
    titles-only overflow, plus the unevaluated-overflow count when the screen ceiling
    truncated the candidate list. Empty string when nothing surfaced."""
    surfaced = [row for row in tally if row.get("verdict") == "A"]
    if not surfaced and screen_overflow <= 0:
        return ""
    lines = [
        _BLOCK_HEADER,
        (
            "The screen below flagged open/in_progress bugs OUTSIDE this epic's hierarchy as "
            "candidate defects in the epic's own deliverable. Adjudicate each per the "
            "'Unresolved bug candidates' rule: retrieve detail with show_ticket as needed."
        ),
    ]
    for row in surfaced[:FORWARD_CAP]:
        lines.append(
            f"- {row['ticket_id']} — {row['title']} (screen: {row['citation'] or 'A, no citation'})"
        )
    extra = surfaced[FORWARD_CAP:]
    if extra:
        titles = "; ".join(row["title"] for row in extra)
        lines.append(f"- plus {len(extra)} more A-verdict candidate(s), titles only: {titles}")
    if screen_overflow > 0:
        lines.append(
            f"- NOTE: {screen_overflow} further qualifying bug(s) exceeded the screen ceiling "
            "and were NOT evaluated (recorded in the sidecar tally)."
        )
    return "\n".join(lines)


def run_screen(
    epic_id: str,
    root_state: dict,
    repo_root,
    *,
    cfg: LLMConfig | None = None,
    runner: Runner | None = None,
    screen_fn: ScreenFn | None = None,
) -> dict:
    """The full stage-2/3 pipeline for one epic close: candidate filter -> screen -> compact
    forwarding block + sidecar tally. Returns ``{"block": str, "tally": list, "overflow": int}``.

    NEVER raises and never blocks the close: any failure degrades open with a logged reason
    and an empty block (the DET caused_by floor is the hard tier; this stage only feeds the
    verifier evidence it would otherwise not see)."""
    from rebar.llm import completion, completion_sidecar

    try:
        candidates, overflow = completion.epic_bug_candidates(epic_id, repo_root)
        if not candidates and overflow == 0:
            return {"block": "", "tally": [], "overflow": 0}
        children_titles = [
            s.get("title", "")
            for s in completion._epic_subtree_states(epic_id, repo_root)[1:]
            if s.get("title")
        ]
        tally = screen_candidates(
            root_state,
            candidates,
            cfg,
            runner,
            screen_fn=screen_fn,
            children_titles=children_titles,
        )
        completion_sidecar.emit_screen_tally(epic_id, tally, overflow=overflow, repo_root=repo_root)
        return {
            "block": candidate_block(tally, screen_overflow=overflow),
            "tally": tally,
            "overflow": overflow,
        }
    except Exception:  # noqa: BLE001 — the screen is advisory; a failure must never block a close
        logger.warning("epic bug screen failed for %s; degrading open (skipped)", epic_id)
        logger.debug("epic bug screen failure detail", exc_info=True)
        return {"block": "", "tally": [], "overflow": 0}
