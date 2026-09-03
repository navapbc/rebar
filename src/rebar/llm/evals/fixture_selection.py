"""Selector + JSONL manifest writer for plan-review regression fixtures (ticket 549b).

Reads a plan-review corpus (``plan_replay/corpus.py`` rows enriched with their slimmed
sidecar findings) and emits a LABELED CANDIDATE MANIFEST: per criterion, each candidate
with its direction, tier, signals and rank, plus a zero-candidate row (with a reason) for
every criterion that produced none. The manifest is JSONL — one JSON object per line, keys
sorted — a byte-stable contract consumed by the diff acceptance test and the downstream
emitter. No YAML, no model call: selection is a pure function of the corpus + git metadata.

Public surface (stable import path ``rebar.llm.evals.fixture_selection``):

- :data:`MIN_MARGIN` — the blocking-tier margin floor.
- :func:`rubric_path` — the rubric file the vintage gate git-logs for a criterion
  (``.rebar/prompts/<pid>.md`` override-wins, else the packaged reviewer ``fallback_file``).
- :func:`last_rubric_commit_ts` — the timestamp of the last commit touching that file on the
  gate's base ref, or ``None`` when there is no committed history / the base ref is absent /
  git errs (the vintage gate then fails closed).
- :func:`select_candidates` — the selector: synthetic-or-real reviews in, manifest rows out.
- :func:`write_manifest` — the byte-stable JSONL writer.

INPUT REVIEW SHAPE (each element of ``reviews``), a dict:
    ``ticket_id``            : str
    ``review_event_ts``      : int   (used for vintage eligibility + consecutive ordering)
    ``review_event_uuid``    : str   (deterministic tie-break)
    ``verdict``              : str
    ``material_fingerprint`` : str   (equal-fingerprint reviews are reproduction pairs)
    ``findings``             : list[slimmed-finding]  (sidecar ``_slim`` shape: ``norm_id``,
                               ``criteria`` list, ``cohort`` list|None, ``decision_margin``
                               float|None, ``decision``, ...)
    ``ticket_state``         : dict  (fed to ``labels.escaped_defect``: ``close_class``,
                               ``inbound_deps``) — OPTIONAL when ``escaped_defect`` is given
    ``escaped_defect``       : bool  (OPTIONAL precomputed answer; wins over
                               ``ticket_state`` so a batched index can replace the
                               per-ticket state compile)

MANIFEST ROW SHAPES (JSON objects, keys sorted on write):
    candidate  : {"kind":"candidate","criterion":str,"direction":"fire"|"no_fire",
                  "norm_id":str|None,"tier":"blocking"|"advisory","rank":int,
                  "signals":[sorted signal names],"escaped_defect":bool,
                  "abs_margin":float|None,"review_event_uuid":str}
    zero       : {"kind":"zero_candidate","criterion":str,"reason":str}

Reasons for a zero-candidate row:
    "no-committed-prompt-history"   — no committed rubric history / absent base ref / git err
    "unreliable-criterion:<id>"     — an open unreliable-criterion ticket skips the criterion
    "no-admitted-candidate"         — eligible, non-skipped, but every candidate was rejected
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import Any

MIN_MARGIN = 0.15

# Tier-determining fire signals (escaped_defect is a PRIORITY signal, not one of these).
FIRE_SIGNALS = ("reproduction_consensus", "author_response", "margin")

# The rubric catalog's canonical location WITHIN a rebar checkout (the `rebar.llm.reviewers`
# package as it is tracked in git). The vintage gate git-logs this path in ``repo_root`` — it
# must NOT use the installed catalog dir, which lands outside ``repo_root`` (and thus outside
# git history) when rebar is installed as a wheel.
_REVIEWERS_REPO_SUBPATH = Path("src") / "rebar" / "llm" / "reviewers"


def rubric_path(criterion_id: str, *, repo_root: str, gate_key: str = "plan_review") -> Path:
    """Return the rubric file the vintage gate git-logs for ``criterion_id``.

    Resolves ``criterion_prompt_id(criterion_id, gate_key=...)`` then applies ``get_prompt``'s
    override-wins ordering: ``<repo_root>/.rebar/prompts/<pid>.md`` when that file exists, else
    the packaged reviewer prompt's canonical **in-repo** source location
    (``<repo_root>/src/rebar/llm/reviewers/<fallback_file>``) so the vintage gate can git-log it.
    Falls back to the installed catalog copy only when the in-repo source is absent (e.g.
    ``repo_root`` is not a rebar checkout) — there the vintage git-log honestly finds no history.
    """
    from rebar.llm.criteria.ids import criterion_prompt_id
    from rebar.llm.prompting.prompts import _catalog_dir, get_prompt

    pid = criterion_prompt_id(criterion_id, gate_key=gate_key)
    override = Path(repo_root) / ".rebar" / "prompts" / f"{pid}.md"
    if override.is_file():
        return override

    prompt = get_prompt(pid, repo_root=repo_root)
    if not prompt.fallback_file:
        raise FileNotFoundError(f"no packaged fallback file for prompt {pid!r}")
    in_repo = Path(repo_root) / _REVIEWERS_REPO_SUBPATH / prompt.fallback_file
    if in_repo.is_file():
        return in_repo
    packaged = Path(str(_catalog_dir())) / prompt.fallback_file
    if not packaged.is_file():
        raise FileNotFoundError(f"packaged prompt file not found: {packaged}")
    return packaged


def last_rubric_commit_ts(
    criterion_id: str,
    *,
    repo_root: str,
    base_ref: str = "origin/main",
    gate_key: str = "plan_review",
) -> int | None:
    """Return the epoch-second timestamp of the last commit touching the criterion's rubric
    file on ``base_ref``, or ``None`` when there is no committed history, ``base_ref`` is
    absent (shallow clone, differently-named remote), or git errs. Never raises."""
    from rebar.llm.prompting.prompts import PromptNotFound

    try:
        path = rubric_path(criterion_id, repo_root=repo_root, gate_key=gate_key)
        try:
            logged_path = str(path.resolve().relative_to(Path(repo_root).resolve()))
        except ValueError:
            logged_path = str(path)
        proc = subprocess.run(  # raw-git-ok: read-only history query
            ["git", "-C", repo_root, "log", "-1", "--format=%ct", base_ref, "--", logged_path],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, FileNotFoundError, PromptNotFound):
        return None

    text = proc.stdout.strip()
    if not text:
        return None
    try:
        return int(text.splitlines()[0])
    except ValueError:
        return None


def _default_rubric_ts(
    criterion_id: str,
    *,
    repo_root: str,
    base_ref: str,
    gate_key: str,
) -> int | None:
    """The default vintage timestamp for ``select_candidates``: ``last_rubric_commit_ts``
    (epoch SECONDS) promoted to the epoch-NANOSECOND unit of ``review_event_ts`` so the
    ``> rubric_ts`` comparison is unit-consistent. ``None`` when there is no committed
    history (propagates the no-committed-prompt-history zero row)."""
    seconds = last_rubric_commit_ts(
        criterion_id, repo_root=repo_root, base_ref=base_ref, gate_key=gate_key
    )
    return None if seconds is None else seconds * 1_000_000_000


def _abs_margin(value: Any) -> float | None:
    if value is None:
        return None
    return abs(float(value))


def _review_sort_key(review: dict[str, Any]) -> tuple[int, str]:
    return (int(review["review_event_ts"]), str(review["review_event_uuid"]))


def _eligible_reviews(reviews: list[dict[str, Any]], rubric_ts: int) -> list[dict[str, Any]]:
    return sorted(
        [r for r in reviews if int(r["review_event_ts"]) > rubric_ts],
        key=_review_sort_key,
    )


def _findings_for_criterion(review: dict[str, Any], criterion_id: str) -> list[dict[str, Any]]:
    return [f for f in review.get("findings", []) if criterion_id in (f.get("criteria") or [])]


def _routed_without_fire(review: dict[str, Any], criterion_id: str) -> bool:
    if _findings_for_criterion(review, criterion_id):
        return False
    for finding in review.get("findings", []):
        cohort = finding.get("cohort")
        if isinstance(cohort, list) and criterion_id in cohort:
            return True
    return False


def _has_reproduction(reviews: list[dict[str, Any]]) -> bool:
    fingerprints = Counter(str(r["material_fingerprint"]) for r in reviews)
    return any(count >= 2 for count in fingerprints.values())


def _author_response_norm_ids(eligible: list[dict[str, Any]]) -> set[str]:
    from rebar.llm.evals.plan_replay.labels import classify_finding_survival

    resolved: set[str] = set()
    by_ticket: dict[str, list[dict[str, Any]]] = {}
    for review in eligible:
        by_ticket.setdefault(str(review["ticket_id"]), []).append(review)
    for ticket_reviews in by_ticket.values():
        for prev, curr in pairwise(sorted(ticket_reviews, key=_review_sort_key)):
            if prev["material_fingerprint"] == curr["material_fingerprint"]:
                continue
            labels = classify_finding_survival(prev.get("findings", []), curr.get("findings", []))
            resolved.update(
                norm_id for norm_id, label in labels.items() if label == "resolved_by_author"
            )
    return resolved


def _escaped(review: dict[str, Any]) -> bool:
    """The escaped-defect priority signal for one review's ticket.

    ``escaped_defect`` is a set-UNION over every review backing a candidate (see
    ``_fire_rows``/``_no_fire_rows``: ``any(_escaped(r) for r in ...)``), so the ticket
    population this needs an answer for is the union of ticket ids across ALL backing
    reviews — not one ticket per emitted candidate. A review may carry that answer
    precomputed under ``escaped_defect`` (the batched-index path in
    :func:`select_from_corpus`); otherwise it is derived from the compiled
    ``ticket_state`` the eager per-ticket reader supplied.
    """
    from rebar.llm.evals.plan_replay.labels import escaped_defect

    precomputed = review.get("escaped_defect")
    if precomputed is not None:
        return bool(precomputed)
    return escaped_defect(review.get("ticket_state", {}))


def _representative(items: list[tuple[dict[str, Any], float | None]]) -> tuple[float | None, str]:
    margin, review = min(
        ((margin, review) for review, margin in items),
        key=lambda item: (item[0] is None, -(item[0] or 0.0), str(item[1]["review_event_uuid"])),
    )
    return margin, str(review["review_event_uuid"])


def _candidate_sort_key(row: dict[str, Any]) -> tuple[int, bool, bool, float, str]:
    margin = row["abs_margin"]
    return (
        0 if row["tier"] == "blocking" else 1,
        not bool(row["escaped_defect"]),
        margin is None,
        -(margin or 0.0),
        str(row["review_event_uuid"]),
    )


def _fire_rows(criterion_id: str, eligible: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_norm: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for review in eligible:
        for finding in _findings_for_criterion(review, criterion_id):
            by_norm.setdefault(str(finding["norm_id"]), []).append((review, finding))

    author_responses = _author_response_norm_ids(eligible)
    rows: list[dict[str, Any]] = []
    for norm_id, entries in by_norm.items():
        reviews_for_norm = [review for review, _finding in entries]
        signals: set[str] = set()
        if _has_reproduction(_unique_reviews(reviews_for_norm)):
            signals.add("reproduction_consensus")
        if norm_id in author_responses:
            signals.add("author_response")
        rep_items = [
            (review, _abs_margin(finding.get("decision_margin"))) for review, finding in entries
        ]
        if any(margin is not None and margin >= MIN_MARGIN for _review, margin in rep_items):
            signals.add("margin")
        if not signals:
            continue
        abs_margin, review_uuid = _representative(rep_items)
        tier = "blocking" if set(FIRE_SIGNALS) <= signals else "advisory"
        rows.append(
            {
                "kind": "candidate",
                "criterion": criterion_id,
                "direction": "fire",
                "norm_id": norm_id,
                "tier": tier,
                "rank": 0,
                "signals": sorted(signals),
                "escaped_defect": any(
                    _escaped(review) for review in _unique_reviews(reviews_for_norm)
                ),
                "abs_margin": abs_margin,
                "review_event_uuid": review_uuid,
            }
        )
    return _rank(rows)


def _unique_reviews(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for review in sorted(reviews, key=_review_sort_key):
        uuid = str(review["review_event_uuid"])
        if uuid in seen:
            continue
        seen.add(uuid)
        out.append(review)
    return out


def _no_fire_rows(criterion_id: str, eligible: list[dict[str, Any]]) -> list[dict[str, Any]]:
    silent = [review for review in eligible if _routed_without_fire(review, criterion_id)]
    if not _has_reproduction(silent):
        return []
    abs_margin, review_uuid = _representative([(review, None) for review in silent])
    return [
        {
            "kind": "candidate",
            "criterion": criterion_id,
            "direction": "no_fire",
            "norm_id": None,
            "tier": "advisory",
            "rank": 0,
            "signals": ["reproduction_consensus"],
            "escaped_defect": any(_escaped(review) for review in silent),
            "abs_margin": abs_margin,
            "review_event_uuid": review_uuid,
        }
    ]


def _rank(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(rows, key=_candidate_sort_key)
    for idx, row in enumerate(ranked):
        row["rank"] = idx
    return ranked


def select_candidates(
    reviews: list[dict[str, Any]],
    *,
    criteria_ids: list[str],
    repo_root: str = ".",
    base_ref: str = "origin/main",
    unreliable: dict[str, str] | None = None,
    rubric_history: Callable[[str], int | None] | None = None,
    gate_key: str = "plan_review",
) -> list[dict[str, Any]]:
    """Select labeled candidates for ``criteria_ids`` from ``reviews`` and return manifest rows.

    ``unreliable`` maps a criterion id → the open ``unreliable-criterion`` ticket id that skips
    it (a ``"unreliable-criterion:<id>"`` zero-candidate row). ``rubric_history`` injects the
    vintage gate for tests: a callable criterion→last-rubric-commit-ts (``None`` fails closed);
    when omitted, :func:`last_rubric_commit_ts` is used against ``base_ref``.

    Rows are returned already ordered: grouped per criterion (in ``criteria_ids`` order), and
    within a criterion each direction's candidates in rank order (blocking before advisory,
    then escaped-defect priority, then descending ``abs_margin`` with ``None`` last, then
    ascending ``review_event_uuid``); ``rank`` is the 0-based position within the direction.
    """
    # Boundary normalization (ticket 57c4-4834-2a7a-4a05): drop findings with no ``norm_id``
    # before any norm-keyed consumer sees them. Sidecar events committed before ``norm_id`` was
    # added to the ``_slim`` projection reconstruct findings that lack the key; a finding with no
    # ``norm_id`` has no cross-review identity, so it cannot participate in fire grouping,
    # author-response survival, or churn. Filtering once here covers every subscript site
    # (``_fire_rows`` and, via ``_author_response_norm_ids``, ``classify_finding_survival``/churn)
    # and both public entry points.
    reviews = [
        {
            **review,
            "findings": [f for f in review.get("findings", []) if f.get("norm_id") is not None],
        }
        for review in reviews
    ]
    rows: list[dict[str, Any]] = []
    unreliable = unreliable or {}
    for criterion_id in criteria_ids:
        if criterion_id in unreliable:
            rows.append(
                {
                    "kind": "zero_candidate",
                    "criterion": criterion_id,
                    "reason": f"unreliable-criterion:{unreliable[criterion_id]}",
                }
            )
            continue

        rubric_ts = (
            rubric_history(criterion_id)
            if rubric_history is not None
            else _default_rubric_ts(
                criterion_id, repo_root=repo_root, base_ref=base_ref, gate_key=gate_key
            )
        )
        if rubric_ts is None:
            rows.append(
                {
                    "kind": "zero_candidate",
                    "criterion": criterion_id,
                    "reason": "no-committed-prompt-history",
                }
            )
            continue

        eligible = _eligible_reviews(reviews, rubric_ts)
        fire = _fire_rows(criterion_id, eligible)
        no_fire = _rank(_no_fire_rows(criterion_id, eligible))
        admitted = fire + no_fire
        rows.extend(admitted)
        if not admitted:
            rows.append(
                {
                    "kind": "zero_candidate",
                    "criterion": criterion_id,
                    "reason": "no-admitted-candidate",
                }
            )
    return rows


def _load_cache_rows(cache_dir: Path, content_hash: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with (cache_dir / f"{content_hash}.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _review_findings(tracker_path: str) -> dict[tuple[str, str], list[dict[str, Any]]]:
    from rebar.llm.evals.plan_replay import corpus

    findings: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for ticket_id, by_type in corpus._load_ticket_events(tracker_path).items():
        for event in by_type.get("REVIEW_RESULT", []):
            raw = event.get("data", {}).get("findings")
            findings[(ticket_id, event["uuid"])] = raw if isinstance(raw, list) else []
    return findings


def _has_eval_spec(criterion_id: str, *, repo_root: str, gate_key: str) -> bool:
    from rebar.llm.criteria.ids import criterion_prompt_id
    from rebar.llm.evals.eval import _packaged_eval_spec, eval_spec_path

    pid = criterion_prompt_id(criterion_id, gate_key=gate_key)
    return eval_spec_path(pid, repo_root).is_file() or _packaged_eval_spec(pid).is_file()


def _default_criteria(repo_root: str, gate_key: str) -> list[str]:
    import rebar.llm.plan_review.registry  # noqa: F401
    from rebar.llm.criteria.overlay import effective_routing

    return [
        criterion_id
        for criterion_id in effective_routing(repo_root, gate_key=gate_key)
        if not _has_eval_spec(criterion_id, repo_root=repo_root, gate_key=gate_key)
    ]


def _enrich_escaped_batched(reviews: list[dict[str, Any]], tracker: str) -> None:
    """Stamp each review's ``escaped_defect`` from ONE store-wide index (the default).

    Replaces the per-ticket ``show_ticket(include_inbound=True)`` fan-out: that reader
    was invoked once per review-bearing ticket, and because ``escaped_defect`` unions
    over every review backing a candidate, that population is most of the store's
    review-bearing tickets (measured on rebar's own tracker: 1554 of 1960, 79%), each
    costing an O(store) inbound byte-scan. The index answers all of them in one pass.
    """
    from rebar.llm.evals.plan_replay.labels import build_escaped_ticket_index

    escaped = build_escaped_ticket_index(tracker)
    for review in reviews:
        review["escaped_defect"] = review["ticket_id"] in escaped


def _enrich_escaped_eager(
    reviews: list[dict[str, Any]],
    tracker: str,
    reader: Callable[[str, str], dict[str, Any]],
) -> None:
    """Stamp each review's ``ticket_state`` by compiling it per ticket via ``reader``.

    The pre-batched-index path, kept reachable (and exercised by the byte-identical
    regression test) so the batched default can be diffed against the real original
    rather than against a snapshot generated from the new code.
    """
    state_cache: dict[str, dict[str, Any]] = {}
    for review in reviews:
        ticket_id = review["ticket_id"]
        if ticket_id not in state_cache:
            state_cache[ticket_id] = dict(reader(ticket_id, tracker))
        review["ticket_state"] = state_cache[ticket_id]


def select_from_corpus(
    *,
    repo_root: str = ".",
    tracker_path: str | None = None,
    base_ref: str = "origin/main",
    cache_dir: str | Path,
    criteria_ids: list[str] | None = None,
    gate_key: str = "plan_review",
    read_ticket_state: Callable[[str, str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Assemble reviews from the real committed corpus and return manifest rows.

    Builds corpus rows via ``plan_replay/corpus.py`` (over ``tracker_path``, the tickets
    tracker), enriches each with its slimmed sidecar ``findings`` AND its ticket's
    escaped-defect answer (so that priority signal can fire), restricts to ``criteria_ids``
    when given else the criteria ``criteria.effective_routing(repo_root, gate_key=...)``
    reports with no eval spec, and calls :func:`select_candidates` with the real vintage gate
    (git-log against ``base_ref`` in ``repo_root``).

    The escaped-defect answer comes from ONE store-wide
    ``labels.build_escaped_ticket_index`` pass by default — NOT a per-ticket
    ``show_ticket(include_inbound=True)`` compile. That compile derives its inbound half by
    byte-scanning every event file in the store, so it cost ~5s per ticket and had to run for
    the UNION of ticket ids across every review backing a candidate (``escaped_defect`` unions
    over backing reviews, so that is most of the review-bearing population, not one ticket per
    candidate) — hours before a single row was emitted. Passing ``read_ticket_state``
    (``ticket_id, tracker_path -> state``) selects that eager per-ticket path instead: tests
    inject a stub, and the byte-identical regression test injects the real reader. Pure git +
    local hashing: no model call, no network. Deterministic — two runs over the same
    committed history return byte-identical rows.
    """
    from rebar import config
    from rebar.llm.evals.plan_replay.corpus import build_corpus

    tracker = tracker_path or str(config.tracker_dir(repo_root))
    cache_path = Path(cache_dir)
    manifest = build_corpus({"default": tracker}, cache_dir=cache_path)
    reviews = _load_cache_rows(cache_path, str(manifest["content_hash"]))
    findings = _review_findings(tracker)
    for review in reviews:
        review["findings"] = findings.get((review["ticket_id"], review["review_event_uuid"]), [])
    if read_ticket_state is None:
        _enrich_escaped_batched(reviews, tracker)
    else:
        _enrich_escaped_eager(reviews, tracker, read_ticket_state)

    selected_criteria = criteria_ids or _default_criteria(repo_root, gate_key)

    return select_candidates(
        reviews,
        criteria_ids=selected_criteria,
        repo_root=repo_root,
        base_ref=base_ref,
        gate_key=gate_key,
    )


def write_manifest(rows: list[dict[str, Any]], path: str | Path) -> None:
    """Write ``rows`` as JSONL to ``path`` — one ``json.dumps(row, sort_keys=True)`` per line,
    in the order given, each terminated by a single ``\\n``. Byte-stable for the diff AC."""
    with Path(path).open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
