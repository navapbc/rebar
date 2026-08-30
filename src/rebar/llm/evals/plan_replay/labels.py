"""Derive outcome labels + a reviewer-noise floor from the plan-review replay corpus
(ticket expectable-clownlike-ynambu / 4bd8-a2bb-813b-427e).

The replay corpus (:mod:`rebar.llm.evals.plan_replay.corpus`) freezes *what got reviewed*
-- a ticket's material at each plan-review pass, verified against its signed fingerprint.
This module answers the next question: *did the review's verdict matter?* An
``escaped_defect`` (a bug later traced back to a plan-review pass via ``caused_by``, or
closed ``plan_defect``), a forced or reopened close, or a completion-verifier failure
after a plan-review pass are all outcome signals a downstream eval can regress against.
Consecutive review passes on the *same* ticket also let us measure reviewer noise: two
reviews of byte-identical material (same ``material_fingerprint``) that land different
verdicts (:func:`noise_flip`) are pure reviewer variance, not a real quality signal --
that variance rate is the floor below which no eval improvement is distinguishable from
noise. Reviews of *differing* material instead measure real churn: which findings
persisted vs. were resolved (:func:`classify_finding_survival`), and how much a
criterion's finding set moved (:func:`per_criterion_churn`).

The pure functions here operate on plain dicts/lists reconstructed from ticket event
logs -- no I/O, so they are trivially unit-testable. :func:`build_labels` is the
integration driver: it calls :func:`rebar.llm.evals.plan_replay.corpus.build_corpus` for
the verified row population, walks each ticket's consecutive review pairs, and writes a
labeled JSONL sibling of the corpus cache, hashed the same way so a stale label file
against fresh corpus data is a loud error (:class:`LabelsHashMismatch`) rather than a
silent mismatch.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rebar.llm.evals.plan_replay import corpus

#: Minimum age (in days) a ticket's most recent close must have reached, with no escape
#: signal, before that close counts as "clean" -- a close that just happened has not
#: had time for a defect to surface (get reopened, get a completion-verifier failure,
#: get traced back via ``caused_by``), so calling it clean too early would overstate the
#: plan-review pass's success.
CLEAN_CLOSE_MIN_DAYS = 7

#: Group names ``per_question_agreement`` flattens a finding's ``verification`` dict
#: under, as the ``f"{group}.{key}"`` prefix of each comparison key.
_VERIFICATION_GROUPS = ("binary", "severity_attributes")

_HASH_RE = re.compile(r"labels-([0-9a-f]+)\.jsonl$")


class LabelsHashMismatch(Exception):
    """Raised by :func:`load_labels` when the label file's embedded content hash no
    longer matches a fresh :func:`corpus.build_corpus` run over the same store roots --
    the underlying corpus moved (new reviews landed, history was rewritten) since the
    labels were written, so the file on disk is stale."""


# ── pure functions over plain dicts/lists (no I/O) ──────────────────────────────────


def escaped_defect(ticket_state: dict[str, Any]) -> bool:
    """True if this ticket's own state records a plan-review escape: it was closed as a
    ``plan_defect`` bug itself, or another ticket's bug names it as the culprit via an
    inbound ``caused_by`` link.

    ``caused_by`` is recorded OUTBOUND on the bug, pointing at its culprit -- so the
    culprit only ever sees it INBOUND, via ``ticket_state["inbound_deps"]``.
    """
    if ticket_state.get("close_class") == "plan_defect":
        return True
    inbound_deps = ticket_state.get("inbound_deps") or []
    return any(dep.get("relation") == "caused_by" for dep in inbound_deps)


def escape_signals(*, escaped: bool, completion_failed: bool, reopened: bool, forced: bool) -> bool:
    """Simple boolean OR over the four independent escape signals."""
    return escaped or completion_failed or reopened or forced


def reopen_count(events: list[dict[str, Any]]) -> int:
    """Count of ``STATUS`` events recording a ``closed -> open`` reopen."""
    return sum(
        1
        for e in events
        if e.get("kind") == "STATUS"
        and e.get("data", {}).get("current_status") == "closed"
        and e.get("data", {}).get("status") == "open"
    )


def force_close(events: list[dict[str, Any]]) -> bool:
    """True if any ``COMMENT`` event's body is a ``FORCE_CLOSE:``-prefixed marker."""
    for e in events:
        if e.get("kind") != "COMMENT":
            continue
        body = str(e.get("data", {}).get("body", ""))
        if body.startswith("FORCE_CLOSE:"):
            return True
    return False


def completion_verifier_fail_count(events: list[dict[str, Any]]) -> int:
    """Count of ``COMPLETION_VERDICT`` events carrying a FAIL schema.

    Both pass and fail verdicts share the same event ``kind``
    (``COMPLETION_VERDICT``) -- the outcome is discriminated on
    ``data["schema"]``, never on ``kind`` alone.
    """
    return sum(
        1
        for e in events
        if e.get("kind") == "COMPLETION_VERDICT"
        and e.get("data", {}).get("schema") == "completion_verifier_fail_v1"
    )


def completion_failed_after_pass(pass_ts: int, events: list[dict[str, Any]]) -> bool:
    """True if a completion-verifier FAIL landed strictly after ``pass_ts`` (raw
    nanosecond-epoch comparison)."""
    return any(
        e.get("kind") == "COMPLETION_VERDICT"
        and e.get("data", {}).get("schema") == "completion_verifier_fail_v1"
        and e.get("ts", 0) > pass_ts
        for e in events
    )


def latest_close_ts(events: list[dict[str, Any]]) -> int | None:
    """The ``ts`` of the most recent ``STATUS`` event recording a close, or ``None``.

    Assumes ``events`` is already sorted ascending by ``ts``; the last matching event
    in list order is the most recent one.
    """
    result: int | None = None
    for e in events:
        if e.get("kind") == "STATUS" and e.get("data", {}).get("status") == "closed":
            result = e.get("ts")
    return result


def clean_close(
    *,
    closed: bool,
    escape: bool,
    latest_close_ts_ns: int | None,
    now: datetime,
    min_days: int = CLEAN_CLOSE_MIN_DAYS,
) -> bool:
    """True if the ticket is closed, has no escape signal, and its most recent close is
    old enough (``>= min_days`` ago) for an escape to plausibly have surfaced by now."""
    if not closed or escape or latest_close_ts_ns is None:
        return False
    closed_at = datetime.fromtimestamp(latest_close_ts_ns / 1e9, tz=timezone.utc)
    return (now - closed_at).days >= min_days


def classify_finding_survival(
    review_k: list[dict[str, Any]], review_k1: list[dict[str, Any]]
) -> dict[str, str]:
    """For every finding in ``review_k``, label whether its ``norm_id`` still appears in
    ``review_k1`` (``"persisted"``) or dropped out (``"resolved_by_author"``).

    Covers only ``review_k``'s findings -- a ``norm_id`` newly appearing in
    ``review_k1`` gets no entry.
    """
    k1_ids = {f["norm_id"] for f in review_k1}
    return {
        f["norm_id"]: ("persisted" if f["norm_id"] in k1_ids else "resolved_by_author")
        for f in review_k
    }


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _criterion_sets(review: list[dict[str, Any]]) -> dict[str, set[str]]:
    sets: dict[str, set[str]] = {}
    for f in review:
        for criterion in f.get("criteria", []):
            sets.setdefault(criterion, set()).add(f["norm_id"])
    return sets


def per_criterion_churn(
    review_k: list[dict[str, Any]], review_k1: list[dict[str, Any]]
) -> dict[str, Any]:
    """Per-criterion + mean churn (``1 - jaccard``) of the finding ``norm_id`` sets
    between two reviews, over every criterion id observed in either review."""
    sets_k = _criterion_sets(review_k)
    sets_k1 = _criterion_sets(review_k1)
    criterion_ids = set(sets_k) | set(sets_k1)

    per_criterion: dict[str, float] = {}
    for criterion in criterion_ids:
        churn = 1.0 - _jaccard(sets_k.get(criterion, set()), sets_k1.get(criterion, set()))
        per_criterion[criterion] = churn

    mean = sum(per_criterion.values()) / len(per_criterion) if per_criterion else 0.0
    return {"mean": mean, "per_criterion": per_criterion}


def _flatten_verification(verification: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for group in _VERIFICATION_GROUPS:
        for key, value in (verification.get(group) or {}).items():
            flat[f"{group}.{key}"] = value
    return flat


def per_question_agreement(
    review_k: list[dict[str, Any]], review_k1: list[dict[str, Any]]
) -> dict[str, Any]:
    """Agreement rate between two reviews' per-finding ``verification`` answers, over
    findings sharing a ``norm_id`` in both reviews and carrying ``verification`` in both.
    """
    k_by_id = {f["norm_id"]: f for f in review_k}
    k1_by_id = {f["norm_id"]: f for f in review_k1}

    comparisons = 0
    matches = 0
    for norm_id, f_k in k_by_id.items():
        f_k1 = k1_by_id.get(norm_id)
        if f_k1 is None:
            continue
        verification_k = f_k.get("verification")
        verification_k1 = f_k1.get("verification")
        if not verification_k or not verification_k1:
            continue
        flat_k = _flatten_verification(verification_k)
        flat_k1 = _flatten_verification(verification_k1)
        for key in set(flat_k) & set(flat_k1):
            comparisons += 1
            if flat_k[key] == flat_k1[key]:
                matches += 1

    agreement = matches / comparisons if comparisons else None
    return {"agreement": agreement, "comparisons": comparisons}


def noise_flip(review_k_row: dict[str, Any], review_k1_row: dict[str, Any]) -> bool:
    """True if two corpus rows for identical material landed different verdicts --
    pure reviewer noise, since the material under review did not change."""
    return review_k_row["verdict"] != review_k1_row["verdict"]


# ── ticket-level labels (multi-store, via REBAR_TRACKER_DIR override) ───────────────

#: Event kinds ``_ticket_status_events`` reads -- `_enumerate_event_blobs` applies no
#: content-type filter itself, so a caller wanting anything besides corpus.py's own
#: CREATE/EDIT/FILE_IMPACT/REVIEW_RESULT subset filters the same blob list by ``kind``.
_STATUS_KINDS = {"STATUS", "COMMENT", "COMPLETION_VERDICT"}


def _ticket_status_events(ticket_id: str, tracker_path: str) -> list[dict[str, Any]]:
    """STATUS/COMMENT/COMPLETION_VERDICT events for ``ticket_id``, oldest-first --
    the same git-object-walk :mod:`corpus` uses, filtered to the kinds this module
    needs and never recovered by :func:`corpus.build_corpus` itself."""
    blobs = [
        b
        for b in corpus._enumerate_event_blobs(tracker_path)
        if b.ticket_id == ticket_id and b.kind in _STATUS_KINDS
    ]
    bodies = corpus._batch_cat(tracker_path, [b.sha for b in blobs])
    events: list[dict[str, Any]] = []
    for b in blobs:
        raw = bodies.get(b.sha)
        if raw is None:
            continue
        try:
            data = json.loads(raw).get("data", {})
        except (json.JSONDecodeError, AttributeError):
            data = {}
        events.append({"kind": b.kind, "ts": b.ts, "data": data})
    events.sort(key=lambda e: e["ts"])
    return events


def _read_ticket_state_via_env_override(ticket_id: str, tracker_path: str) -> dict[str, Any]:
    """Read ``ticket_id``'s compiled state from the tracker at ``tracker_path`` via
    ``show_ticket``, by temporarily pointing ``REBAR_TRACKER_DIR`` (which wins verbatim
    over any config, per ``config.tracker_dir_override``) at it -- the only way to target
    an arbitrary ``store_roots`` tracker dir regardless of that store's own config."""
    from rebar import show_ticket

    prior = os.environ.get("REBAR_TRACKER_DIR")  # read-via: multi-store-tracker-relocation
    os.environ["REBAR_TRACKER_DIR"] = tracker_path  # read-via: multi-store-tracker-relocation
    try:
        return dict(show_ticket(ticket_id, include_inbound=True))
    finally:
        if prior is None:
            os.environ.pop("REBAR_TRACKER_DIR", None)
        else:
            os.environ["REBAR_TRACKER_DIR"] = prior  # read-via: multi-store-tracker-relocation


def _ticket_labels_for(
    ticket_id: str,
    tracker_path: str,
    pass_rows: list[dict[str, Any]],
    now: datetime,
    *,
    read_ticket_state: Any = None,
) -> dict[str, Any]:
    """The full ticket-level label set for one ticket: ``escaped_defect``,
    ``completion_failed_after_pass``, ``reopened``, ``force_close``, ``clean_close``,
    plus the raw counts each derives from.

    ``read_ticket_state`` defaults to :func:`_read_ticket_state_via_env_override`
    (the real, multi-store-safe reader); tests inject a stub to avoid needing a full
    reducer-compatible fake tracker.
    """
    reader = read_ticket_state or _read_ticket_state_via_env_override
    state = reader(ticket_id, tracker_path)
    events = _ticket_status_events(ticket_id, tracker_path)

    escaped = escaped_defect(state)
    completion_failed = any(
        completion_failed_after_pass(row["review_event_ts"], events)
        for row in pass_rows
        if row.get("verdict") == "PASS"
    )
    reopen_n = reopen_count(events)
    reopened_flag = reopen_n > 0
    forced = force_close(events)
    escape = escape_signals(
        escaped=escaped, completion_failed=completion_failed, reopened=reopened_flag, forced=forced
    )
    closed = state.get("status") == "closed"
    close_ts = latest_close_ts(events)
    clean = clean_close(closed=closed, escape=escape, latest_close_ts_ns=close_ts, now=now)

    return {
        "escaped_defect": escaped,
        "completion_failed_after_pass": completion_failed,
        "reopened": reopened_flag,
        "reopen_count": reopen_n,
        "force_close": forced,
        "completion_verifier_fail_count": completion_verifier_fail_count(events),
        "clean_close": clean,
    }


def render_report(
    labeled_rows: list[dict[str, Any]], ticket_labels: dict[str, dict[str, Any]]
) -> str:
    """A Markdown summary of per-label-source counts, for the committed labels report."""
    pair_counts: dict[str, int] = {}
    for row in labeled_rows:
        pair = row.get("pair_with_previous")
        if not pair:
            continue
        kind = pair.get("pair_kind")
        if kind:
            pair_counts[kind] = pair_counts.get(kind, 0) + 1

    ticket_counts: dict[str, int] = {}
    for tlabels in ticket_labels.values():
        for key in ("escaped_defect", "completion_failed_after_pass", "reopened", "clean_close"):
            if tlabels.get(key):
                ticket_counts[key] = ticket_counts.get(key, 0) + 1

    lines = [
        "# Plan-review outcome labels",
        "",
        f"Verified rows: {len(labeled_rows)}",
        f"Tickets: {len(ticket_labels)}",
        "",
        "## Finding-level (pair) labels",
        "",
    ]
    if pair_counts:
        for kind in sorted(pair_counts):
            lines.append(f"- {kind}: {pair_counts[kind]}")
    else:
        lines.append("- (no consecutive review pairs in this corpus)")
    lines += ["", "## Ticket-level labels", ""]
    if ticket_counts:
        for key in sorted(ticket_counts):
            lines.append(f"- {key}: {ticket_counts[key]}")
    else:
        lines.append("- (no ticket-level escape signals observed)")
    return "\n".join(lines) + "\n"


# ── integration driver (uses corpus.py + git-object-walk + show_ticket) ─────────────


def _load_cache_rows(cache_dir: Path, content_hash: str) -> list[dict[str, Any]]:
    cache_path = cache_dir / f"{content_hash}.jsonl"
    rows: list[dict[str, Any]] = []
    with cache_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _findings_for_row(
    row: dict[str, Any],
    event_index: dict[tuple[str, str], corpus._Blob],
    tracker_paths: dict[str, str],
) -> list[dict[str, Any]]:
    """Best-effort re-read of a review row's finding list from its REVIEW_RESULT body.

    Matched by ``(ticket_id, review_event_uuid)`` against the tracker's full blob
    enumeration. An unmatched or unreadable body degrades to an empty finding list
    rather than raising -- this driver's job is to always produce a valid labels file,
    not to guarantee finding-level detail for every pair.
    """
    key = (row["ticket_id"], row["review_event_uuid"])
    blob = event_index.get(key)
    if blob is None:
        return []
    tracker_path = tracker_paths.get(row["store"])
    if tracker_path is None:
        return []
    bodies = corpus._batch_cat(tracker_path, [blob.sha])
    raw = bodies.get(blob.sha)
    if raw is None:
        return []
    try:
        data = json.loads(raw).get("data", {})
    except (json.JSONDecodeError, AttributeError):
        return []
    findings = data.get("findings")
    return findings if isinstance(findings, list) else []


def _build_event_index(store_roots: dict[str, str]) -> dict[tuple[str, str], corpus._Blob]:
    index: dict[tuple[str, str], corpus._Blob] = {}
    for tracker_path in store_roots.values():
        for blob in corpus._enumerate_event_blobs(tracker_path):
            if blob.kind == "REVIEW_RESULT":
                index[(blob.ticket_id, blob.uuid)] = blob
    return index


def _label_pair(
    prev: dict[str, Any],
    curr: dict[str, Any],
    event_index: dict[tuple[str, str], corpus._Blob],
    tracker_paths: dict[str, str],
) -> dict[str, Any]:
    if prev["material_fingerprint"] == curr["material_fingerprint"]:
        return {"pair_kind": "identical_material", "noise_flip": noise_flip(prev, curr)}

    findings_prev = _findings_for_row(prev, event_index, tracker_paths)
    findings_curr = _findings_for_row(curr, event_index, tracker_paths)
    return {
        "pair_kind": "differing_material",
        "finding_survival": classify_finding_survival(findings_prev, findings_curr),
        "criterion_churn": per_criterion_churn(findings_prev, findings_curr),
        "question_agreement": per_question_agreement(findings_prev, findings_curr),
    }


def build_labels(
    store_roots: dict[str, str],
    *,
    cache_dir: Path | str,
    out_dir: Path | str,
    now: datetime | None = None,
    read_ticket_state: Any = None,
) -> dict[str, Any]:
    """Derive outcome + reviewer-noise labels over the verified corpus population.

    Calls :func:`corpus.build_corpus` for the ticket population (writing/reusing its
    cache), groups the resulting rows by ``ticket_id``, and labels every consecutive
    pair of a ticket's reviews (ordered by ``review_event_ts``) -- identical-material
    pairs via :func:`noise_flip`, differing-material pairs via a best-effort re-read of
    each review's finding list. Also computes each ticket's ticket-level labels
    (:func:`_ticket_labels_for`) via ``show_ticket``. Writes every source row (each
    augmented with its ticket labels and whatever pair label applied) as JSONL to
    ``out_dir / f"labels-{content_hash}.jsonl"``, plus a Markdown report
    (:func:`render_report`) to ``out_dir / f"labels-report-{content_hash}.md"``.

    ``now``/``read_ticket_state`` are test seams -- production callers leave both at
    their defaults (real wall-clock time, the real multi-store ``show_ticket`` reader).
    """
    now = now or datetime.now(timezone.utc)
    manifest = corpus.build_corpus(store_roots, cache_dir=cache_dir)
    content_hash = manifest["content_hash"]
    rows = _load_cache_rows(Path(cache_dir), content_hash)

    by_ticket: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_ticket.setdefault(row["ticket_id"], []).append(row)
    for ticket_rows in by_ticket.values():
        ticket_rows.sort(key=lambda r: r["review_event_ts"])

    event_index = _build_event_index(store_roots)

    ticket_labels: dict[str, dict[str, Any]] = {}
    for ticket_id, ticket_rows in by_ticket.items():
        store_name = ticket_rows[0]["store"]
        tracker_path = store_roots.get(store_name)
        if tracker_path is None:
            continue
        ticket_labels[ticket_id] = _ticket_labels_for(
            ticket_id, tracker_path, ticket_rows, now, read_ticket_state=read_ticket_state
        )

    labeled_rows: list[dict[str, Any]] = []
    for ticket_id, ticket_rows in by_ticket.items():
        for i, row in enumerate(ticket_rows):
            out_row = dict(row)
            out_row["ticket_labels"] = ticket_labels.get(ticket_id, {})
            if i > 0:
                out_row["pair_with_previous"] = _label_pair(
                    ticket_rows[i - 1], row, event_index, store_roots
                )
            labeled_rows.append(out_row)

    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)
    out_path = out_dir_path / f"labels-{content_hash}.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        for row in labeled_rows:
            fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")

    report_path = out_dir_path / f"labels-report-{content_hash}.md"
    report_path.write_text(render_report(labeled_rows, ticket_labels), encoding="utf-8")

    return {
        "content_hash": content_hash,
        "row_count": len(labeled_rows),
        "ticket_count": len(ticket_labels),
    }


def load_labels(
    path: str, *, store_roots: dict[str, str], cache_dir: Path | str
) -> list[dict[str, Any]]:
    """Load a labels JSONL file written by :func:`build_labels`, after confirming its
    embedded content hash still matches a fresh :func:`corpus.build_corpus` run over the
    same store roots. Raises :class:`LabelsHashMismatch` on a stale file."""
    m = _HASH_RE.search(path)
    if not m:
        raise LabelsHashMismatch(f"cannot extract a content hash from labels path {path!r}")
    file_hash = m.group(1)

    manifest = corpus.build_corpus(store_roots, cache_dir=cache_dir)
    fresh_hash = manifest["content_hash"]
    if file_hash != fresh_hash:
        raise LabelsHashMismatch(
            f"labels file hash {file_hash} does not match fresh corpus hash {fresh_hash}"
        )

    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows
