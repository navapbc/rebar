"""The epic-close bug screen: haiku relevance triage over out-of-hierarchy bugs (ticket 4b54).

Agents file bugs OUTSIDE an epic's hierarchy during epic execution and deem them
out-of-scope/pre-existing even when they are defects in the epic's own deliverable; the
direct-children close gate cannot see them (event-precise backtest over 56 epic closes: 2 real
at-close escapes, 30a2 and 5b09). The gate is three-staged, cheapest-first:

1. **DET caused_by floor** (:func:`epic_bug_floor_findings`, below) — an
   open/in_progress bug with a ``caused_by`` edge into the subtree deterministically blocks
   the close (enforced in ``gate_ops.completion_precheck``, no LLM call). The hard tier.
2. **DET candidate filter** (:func:`epic_bug_candidates`, below) — status/type
   + (created-after-first-claim OR linked-any-relation-any-depth, both directions), ceiling
   :data:`EPIC_BUG_SCREEN_CEILING`.
3. **The LLM screen** — one single-turn TRIVIAL-class (haiku-tier) call per candidate, forced
   choice ``A`` (defect in something this epic changed/built) / ``B`` (same subsystem,
   pre-existing or adjacent) / ``C`` (unrelated) + one-line citation. A-verdicts are forwarded
   to the completion verifier as a compact evidence block (:data:`FORWARD_CAP` rows of
   title + citation + id, ~40 tokens each) appended INSIDE the precheck-assembled fenced
   context; the verifier adjudicates disposition via its native read-only ``show_ticket``
   tool. A false NEGATIVE here is no worse than today (the bug was invisible before); a false
   POSITIVE costs the verifier one adjudication.

The screen degrades OPEN on non-systemic failures — malformed output, store read error —
(logged, candidate treated as ``C`` / screen skipped), with ONE deliberate exception (bug
1019, operator-ratified fail-closed): a SYSTEMIC provider error (:class:`LLMUnavailableError`,
subclass ``LLMConfigError`` included) PROPAGATES so the close gate's existing fail-closed
handler blocks the close. Degrading it per-candidate silently blinded the caused_by bug floor
when a Bedrock ValidationException failed all 32 screen calls of one close while the verifier
PASSed. Unlike the ``plan_review/pass1.py`` container fan-out (which degrades a fanned-out
worker's systemic error because an upstream chunk pool already re-raises), this screen has no
upstream tripwire — its own calls are the ONLY place the outage can surface — so the warm
call and the fan-out both re-raise. The tally
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
from rebar.llm.errors import LLMUnavailableError
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
# The non-surfacing degrade verdict: a NON-SYSTEMIC per-candidate failure coerces to C
# (unrelated), so a broken screen can only UNDER-report — it never fabricates an A-candidate.
# A systemic provider error (LLMUnavailableError) is the bug-1019 carve-out: it re-raises so
# the close FAILS CLOSED instead of the floor being silently blinded.
_DEGRADE = {"verdict": "C", "citation": ""}

ScreenFn = Callable[[dict, str], dict]


# Enforced screen ceiling for the epic-close bug screen (ticket 4b54): at most this many
# candidates are LLM-evaluated per close — linked-to-subtree candidates first, then by created
# timestamp descending — and the remainder is recorded as an unevaluated-overflow count in the
# sidecar tally (visible to the operator, never silently dropped). Backtested fan-out over 56
# epic closes: median 3, p90 11, max 24, so 32 clears every observed close.
EPIC_BUG_SCREEN_CEILING = 32


def _epic_subtree_states(ticket_id: str, repo_root) -> list[dict]:
    """Compiled states of the epic + every descendant (parent links, any depth), BFS."""
    from rebar import _reads

    root = _reads.show_ticket(ticket_id, repo_root=repo_root)
    out: list[dict] = [root]
    seen = {root.get("ticket_id", ticket_id)}
    frontier = [root.get("ticket_id", ticket_id)]
    while frontier:
        next_frontier: list[str] = []
        for pid in frontier:
            for child in _reads.list_tickets(parent=pid, repo_root=repo_root):
                cid = child.get("ticket_id")
                if cid and cid not in seen:
                    seen.add(cid)
                    out.append(child)
                    next_frontier.append(cid)
        frontier = next_frontier
    return out


def _open_bugs(repo_root) -> list[dict]:
    """Every open or in_progress bug in the store (the only tickets the screen may see)."""
    from rebar import _reads

    bugs: list[dict] = []
    for status in ("open", "in_progress"):
        bugs.extend(_reads.list_tickets(status=status, ticket_type="bug", repo_root=repo_root))
    return bugs


def _first_in_progress_ns(ticket_id: str, repo_root) -> int | None:
    """ns-epoch timestamp of the ticket's FIRST ``open -> in_progress`` STATUS event.

    Read WITH retired tombstones (the ``event_metrics._event_files`` idiom,
    ``include_retired=True``) — snapshot compaction folds old events, so the compiled state
    alone cannot supply this anchor. ``None`` when no such event exists (never claimed)."""
    import json as _json
    import os as _os

    from rebar import config as _config
    from rebar.metrics.event_metrics import _event_files

    ticket_dir = _os.path.join(str(_config.tracker_dir(repo_root)), ticket_id)
    if not _os.path.isdir(ticket_dir):
        return None
    for path in _event_files(ticket_dir, "STATUS", include_retired=True):
        try:
            with open(path, encoding="utf-8") as handle:
                event = _json.load(handle)
        except (OSError, ValueError):
            continue
        data = event.get("data") or {}
        if data.get("current_status") == "open" and data.get("status") == "in_progress":
            ts = event.get("timestamp")
            if isinstance(ts, int):
                return ts
    return None


def epic_bug_floor_findings(ticket_id: str, repo_root) -> list[dict]:
    """The deterministic ``caused_by`` floor of the epic-close bug screen (ticket 4b54).

    Any OPEN/IN_PROGRESS bug carrying a ``caused_by`` edge into the epic's subtree (the epic
    or any descendant) deterministically blocks the epic's close, exactly like an unclosed
    direct child — semantically unambiguous (the bug RECORDS this work broke it), 0 false
    positives over the 56-close backtest. ``caused_by`` is DIRECTIONAL and lives on the BUG's
    own compiled ``deps[]`` with no reciprocal on the epic side, so the floor enumerates the
    open bugs and scans EACH BUG's deps — the epic's own deps cannot supply these edges.
    Returns blocking findings shaped exactly like :func:`child_closure_findings`'s."""
    subtree = {
        s.get("ticket_id") for s in _epic_subtree_states(ticket_id, repo_root) if s.get("ticket_id")
    }
    found: list[dict] = []
    for bug in _open_bugs(repo_root):
        bid = bug.get("ticket_id")
        if bid is None or bid in subtree:
            continue  # in-hierarchy bugs belong to the direct-children gate
        for dep in bug.get("deps") or []:
            if dep.get("relation") != "caused_by":
                continue
            target = dep.get("target_id")
            if target not in subtree:
                continue
            title = (bug.get("title") or "")[:50]
            status = bug.get("status")
            found.append(
                {
                    "criterion": f"no open caused_by bug against the subtree of {ticket_id}",
                    "severity": "high",
                    "dimension": "completion",
                    "detail": (
                        f"bug {bid} ('{title}') is '{status}' and records caused_by -> "
                        f"{target} inside this epic's subtree — the epic's own work broke it. "
                        "Before closing the epic: fix (close) the bug, re-parent it under the "
                        "epic as delegated work, or dispute the caused_by link if it is wrong."
                    ),
                    "citations": [
                        {
                            "kind": "source",
                            "description": f"ticket {bid} caused_by {target}; status={status}",
                        }
                    ],
                }
            )
            break  # one finding per bug, however many subtree edges it carries
    return found


def epic_bug_candidates(ticket_id: str, repo_root) -> tuple[list[dict], int]:
    """The deterministic candidate filter of the epic-close bug screen (ticket 4b54).

    Candidates = OPEN/IN_PROGRESS bugs OUTSIDE the epic's subtree that are (a) created after
    the epic's FIRST ``open -> in_progress`` transition (fallback: the epic's ``created_at``
    when it was never claimed — a wider window is the safe direction, over-inclusion only
    feeds the cheap screen), OR (b) linked by ANY relation, EITHER direction, to the epic or
    any descendant. Commit-relation matching was evaluated and REJECTED (0 unique catches,
    2 FPs on the backtest). Returns ``(candidates, unevaluated_overflow)`` — at most
    :data:`EPIC_BUG_SCREEN_CEILING` candidates, linked-to-subtree first, then created
    descending; the remainder is counted, never silently dropped."""
    states = _epic_subtree_states(ticket_id, repo_root)
    subtree = {s.get("ticket_id") for s in states if s.get("ticket_id")}
    incoming: set[str] = set()  # ids that SUBTREE members link out to (epic-side edges)
    for s in states:
        for dep in s.get("deps") or []:
            target = dep.get("target_id")
            if target:
                incoming.add(target)
    anchor = _first_in_progress_ns(ticket_id, repo_root)
    if anchor is None:
        root = states[0] if states else {}
        anchor = root.get("created_at") or 0
    qualifying: list[tuple[bool, int, dict]] = []
    for bug in _open_bugs(repo_root):
        bid = bug.get("ticket_id")
        if bid is None or bid in subtree:
            continue
        linked = bid in incoming or any(
            dep.get("target_id") in subtree for dep in bug.get("deps") or []
        )
        created = bug.get("created_at") or 0
        if linked or created > anchor:
            qualifying.append((linked, created, bug))
    qualifying.sort(key=lambda q: (0 if q[0] else 1, -q[1]))
    kept = [bug for _linked, _created, bug in qualifying[:EPIC_BUG_SCREEN_CEILING]]
    return kept, max(0, len(qualifying) - len(kept))


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
    cfg = replace(cfg, model=resolve_model_string(TRIVIAL_CLASS, cfg.repo_path))
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
    entangled with bin-packing and budget records). A NON-SYSTEMIC per-candidate failure
    degrades THAT candidate to ``C`` — including the warm call: an unwarmed fan-out only
    forfeits cache hits, and the trivial tier is cheap uncached (bug 7a79 note). A systemic
    provider error (:class:`LLMUnavailableError`) instead PROPAGATES from warm call and
    fan-out alike (bug 1019 fail-closed ruling; see the module docstring for why this
    deliberately diverges from pass1's fan-out degrade).

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
        except LLMUnavailableError:
            # Bug 1019 (operator-ratified FAIL CLOSED): a systemic provider error — the
            # provider rejected/unreachable, or the extra/key absent (LLMConfigError is a
            # subclass by design) — must NOT be laundered into C. Re-raise (warm call and
            # fan-out alike; pool.map surfaces the first worker exception) so the close
            # gate's fail-closed handler blocks the close and names the provider failure.
            raise
        except Exception:
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

    NEVER raises on a NON-SYSTEMIC failure and never blocks the close for one: such a
    failure degrades open with a logged reason and an empty block (the DET caused_by floor
    is the hard tier; this stage only feeds the verifier evidence it would otherwise not
    see). The ONE exception (bug 1019, operator-ratified): a systemic provider error
    (:class:`LLMUnavailableError`) re-raises so the close gate FAILS CLOSED — a blind
    screen must not report success."""
    from rebar.llm import completion_sidecar

    try:
        candidates, overflow = epic_bug_candidates(epic_id, repo_root)
        if not candidates and overflow == 0:
            return {"block": "", "tally": [], "overflow": 0}
        children_titles = [
            s.get("title", "")
            for s in _epic_subtree_states(epic_id, repo_root)[1:]
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
    except LLMUnavailableError:
        raise  # bug 1019: a systemic provider error fails the close CLOSED, never degrades
    except Exception:
        logger.warning("epic bug screen failed for %s; degrading open (skipped)", epic_id)
        logger.debug("epic bug screen failure detail", exc_info=True)
        return {"block": "", "tally": [], "overflow": 0}
