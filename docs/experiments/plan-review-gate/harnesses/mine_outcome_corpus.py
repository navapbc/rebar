#!/usr/bin/env python3
"""E1 outcome-corpus miner (story doctrinal-untruthful-vaquita / e95e; epic
pastoral-aquatic-viper).

Freezes ONE row per REVIEWED ticket (a ticket carrying >=1 REVIEW_RESULT plan-review
sidecar — the §5.1 population, ~527) with the fields E2/E3/E6/R7 join on:

    {ticket_id, ticket_type, level, post_claim_edit_class,
     reopen_count, force_close, completion_verifier_fail_count,
     review_round_count, had_persisted_review}

Provenance / reproducibility — WHY a git-object walk, not an on-disk scan
-------------------------------------------------------------------------
The on-disk ``.tickets-tracker/<ticket_id>/`` directory is COMPACTED: for older
tickets, genesis (CREATE) and transition (STATUS) events are folded into a single
``*-SNAPSHOT.json`` and their individual event files are **deleted with no ``.retired``
copy left on disk**. Measured on this store: 140 of 631 reviewed tickets (22%) have a
SNAPSHOT but zero STATUS files on disk — so an on-disk scan would silently compute
``reopen_count=0`` and ``post_claim_edit_class="none"`` for ~1 in 5 rows.

The authoritative recovery (report §5's method) is therefore a raw git-object walk of
the tickets branch: ``git rev-list --objects --all`` enumerates every event blob ever
committed (the folded events persist as unreachable-from-HEAD but history-reachable
blobs), and ``git cat-file --batch`` reads them. dc58-af7b (a fully-compacted §5 case
ticket) has ZERO STATUS files on disk yet the git-object walk recovers its 1 CREATE +
2 STATUS + 4 EDIT events verbatim. This miner reads events ONLY from git objects, so
it is reproducible from the committed tickets branch alone (no ``~/.claude/jobs/*``
artifact, no compaction dependence).

Data-shape facts this miner relies on (grounded in src/, verified against the store):
  * every event blob is ``<ticket_id>/<ns_ts>-<uuid>-<TYPE>.json[.retired]``; the path
    yields ticket_id, ns timestamp, uuid, and TYPE without reading the blob.
  * transitions are all ``STATUS`` events; ``data = {status, current_status, ...}``.
    A reopen is ``current_status=="closed" and status=="open"`` (rebar reopen ==
    transition closed->open). There is NO REOPEN / FORCE_CLOSE event type.
  * a force-close leaves no STATUS flag — it writes a ``COMMENT`` whose body starts
    with ``"FORCE_CLOSE:"`` (transition_close.py) + a missing completion signature.
  * a completion-verifier FAIL is a ``COMPLETION_VERDICT`` event with
    ``data.schema == "completion_verifier_fail_v1"`` (completion_sidecar.py).
  * a CREATE carries ``data.ticket_type`` and ``data.description``; an EDIT carries its
    change map in ``data.fields`` (e.g. ``fields.description``, ``fields.assignee``).
  * review rounds == distinct REVIEW_RESULT event uuids in history. The git-object walk
    recovers the TRUE historical round count, which may EXCEED the on-disk retention cap
    (50/ticket); this is a strict accuracy gain and is noted in the README.

Usage:
    python mine_outcome_corpus.py            # write runs/outcome_corpus.jsonl (atomic)
    python mine_outcome_corpus.py --dry-run  # compute + validate, write nothing, exit 0
    python mine_outcome_corpus.py --verify-s5  # print the §5 re-derivation table
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections import Counter, defaultdict
from typing import Any

# ── locations ────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", "..", ".."))
TRACKER = os.path.join(REPO_ROOT, ".tickets-tracker")
OUT_DIR = os.path.join(_HERE, "..", "runs")
OUT_PATH = os.path.abspath(os.path.join(OUT_DIR, "outcome_corpus.jsonl"))

# Pre-flight completeness floor: expected ~527 reviewed tickets (report §5.1). If the
# git-object walk recovers fewer, history is incomplete (gc/pruned) — halt, write nothing.
REVIEWED_FLOOR = 500

LEVEL = {"epic": "epic", "story": "story", "task": "task", "bug": "bug"}

# ``<ticket_id>/<ns_ts>-<uuid>-<TYPE>.json`` (optional ``.retired`` suffix). ticket_id is
# four hex quads; TYPE is upper-snake. The path alone yields ts/uuid/TYPE — no blob read.
_PATH_RE = re.compile(
    r"^(?P<tid>[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4})/"
    r"(?P<ts>\d+)-(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-"
    r"(?P<type>[A-Z_]+)\.json(?:\.retired)?$"
)

# Event TYPEs whose *content* we must read (all others we only count by uuid).
_CONTENT_TYPES = {"CREATE", "STATUS", "EDIT", "COMMENT", "COMPLETION_VERDICT"}


# ── git-object event walk (authoritative; compaction-proof) ───────────────────
def _git_stdout(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", TRACKER, *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _enumerate_event_blobs() -> list[tuple[str, str, int, str, str]]:
    """Return ``(sha, ticket_id, ts, uuid, TYPE)`` for every event blob in tickets-branch
    history (``git rev-list --objects --all``), deduped by (ticket_id, uuid)."""
    out = _git_stdout("rev-list", "--objects", "--all")
    seen: set[tuple[str, str]] = set()
    blobs: list[tuple[str, str, int, str, str]] = []
    for line in out.splitlines():
        # "<sha> <path>"; ticket paths never contain spaces.
        sha, _, path = line.partition(" ")
        if not path:
            continue
        m = _PATH_RE.match(path)
        if not m:
            continue
        tid, uuid, typ = m["tid"], m["uuid"], m["type"]
        key = (tid, uuid)
        if key in seen:  # same event across commits / active+retired → one blob
            continue
        seen.add(key)
        blobs.append((sha, tid, int(m["ts"]), uuid, typ))
    return blobs


def _batch_cat(shas: list[str]) -> dict[str, bytes]:
    """Read many blobs in one ``git cat-file --batch`` call; return {sha: content}.

    stdin is fed by a background thread while the main thread drains stdout — without
    this, writing all requests before reading any reply deadlocks once git's output
    fills the OS pipe buffer (the content here is many MB)."""
    if not shas:
        return {}
    proc = subprocess.Popen(
        ["git", "-C", TRACKER, "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout

    def _feed() -> None:
        try:
            proc.stdin.write(("\n".join(shas) + "\n").encode())
        finally:
            proc.stdin.close()

    writer = threading.Thread(target=_feed, daemon=True)
    writer.start()

    contents: dict[str, bytes] = {}
    for _ in shas:
        header = proc.stdout.readline().decode()
        parts = header.split()
        if len(parts) != 3:  # "<sha> missing" or malformed — skip defensively
            continue
        oid, _otype, size = parts[0], parts[1], int(parts[2])
        body = proc.stdout.read(size)
        proc.stdout.read(1)  # trailing newline
        contents[oid] = body
    writer.join()
    proc.wait()
    return contents


def load_events() -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Walk git objects once; return ``{ticket_id: {TYPE: [event, ...]}}`` for the
    REVIEWED population only (tickets with >=1 REVIEW_RESULT blob), each event
    ``{"uuid", "ts", "data"}`` list sorted oldest-first. REVIEW_RESULT (and any
    non-content TYPE) is recorded as a ``{"uuid","ts"}`` stub (counted, not parsed).

    Restricting to reviewed tickets before reading blob content keeps the (multi-MB)
    ``git cat-file`` payload small — the miner's population is the reviewed set anyway.
    """
    blobs = _enumerate_event_blobs()
    reviewed = {b[1] for b in blobs if b[4] == "REVIEW_RESULT"}
    blobs = [b for b in blobs if b[1] in reviewed]
    content_shas = [b[0] for b in blobs if b[4] in _CONTENT_TYPES]
    bodies = _batch_cat(content_shas)

    store: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for sha, tid, ts, uuid, typ in blobs:
        ev: dict[str, Any] = {"uuid": uuid, "ts": ts}
        if typ in _CONTENT_TYPES:
            raw = bodies.get(sha)
            if raw is None:
                continue
            try:
                ev["data"] = json.loads(raw).get("data", {})
            except (json.JSONDecodeError, AttributeError):
                ev["data"] = {}
        store[tid][typ].append(ev)
    for by_type in store.values():
        for evs in by_type.values():
            evs.sort(key=lambda e: e["ts"])
    return store


# ── per-ticket field derivations (operate on one ticket's {TYPE: [events]}) ───
def _evs(tk: dict[str, list[dict[str, Any]]], typ: str) -> list[dict[str, Any]]:
    return tk.get(typ, [])


def reopen_count(tk: dict[str, list[dict[str, Any]]]) -> int:
    return sum(
        1
        for e in _evs(tk, "STATUS")
        if e.get("data", {}).get("current_status") == "closed"
        and e.get("data", {}).get("status") == "open"
    )


def force_close(tk: dict[str, list[dict[str, Any]]]) -> bool:
    return any(
        str(e.get("data", {}).get("body", "")).startswith("FORCE_CLOSE:")
        for e in _evs(tk, "COMMENT")
    )


def completion_verifier_fail_count(tk: dict[str, list[dict[str, Any]]]) -> int:
    return sum(
        1
        for e in _evs(tk, "COMPLETION_VERDICT")
        if e.get("data", {}).get("schema") == "completion_verifier_fail_v1"
    )


def review_round_count(tk: dict[str, list[dict[str, Any]]]) -> int:
    """Distinct REVIEW_RESULT event uuids in history (true count; may exceed the on-disk
    retention cap of 50 — the git-object walk recovers pre-retention rounds)."""
    return len({e["uuid"] for e in _evs(tk, "REVIEW_RESULT")})


def _create_event(tk: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    ces = _evs(tk, "CREATE")
    return ces[0] if ces else None


def ticket_type(tk: dict[str, list[dict[str, Any]]]) -> str | None:
    ce = _create_event(tk)
    if not ce:
        return None
    data = ce.get("data", {})
    return data.get("ticket_type") or data.get("type")


def _claim_and_close_ts(
    tk: dict[str, list[dict[str, Any]]],
) -> tuple[int | None, int | None]:
    """(first STATUS->in_progress ts, first STATUS->closed ts after that claim) — the
    first claim->close cycle window used for post-claim-edit classification."""
    claim_ts = close_ts = None
    for e in _evs(tk, "STATUS"):
        st = e.get("data", {}).get("status")
        if st == "in_progress" and claim_ts is None:
            claim_ts = e["ts"]
        elif st == "closed" and close_ts is None and claim_ts is not None:
            close_ts = e["ts"]
    return claim_ts, close_ts


def _edit_fields(ev: dict[str, Any]) -> dict[str, Any]:
    f = ev.get("data", {}).get("fields", {})
    return f if isinstance(f, dict) else {}


_CHECKBOX_RE = re.compile(r"- \[[ xX]\]")


def _norm_checkboxes(s: str) -> str:
    """Collapse checkbox STATE so that ticking ``- [ ]`` -> ``- [x]`` is invisible; a
    progress check-off must not read as a plan change."""
    return _CHECKBOX_RE.sub("- [ ]", s)


def _ac_block(desc: str) -> str:
    """The ``## Acceptance Criteria`` section (to the next ``## `` heading), checkbox
    state normalized. Empty string if the ticket has no AC block."""
    low = desc.lower()
    i = low.find("## acceptance criteria")
    if i < 0:
        return ""
    rest = desc[i + len("## acceptance criteria") :]
    nxt = re.search(r"\n##\s", rest)
    body = rest[: nxt.start()] if nxt else rest
    return _norm_checkboxes(body).strip()


def _description_timeline(
    tk: dict[str, list[dict[str, Any]]],
) -> list[tuple[int, str, str]]:
    """Reconstruct ``(ts, prev_desc, new_desc)`` for every description-mutating event,
    oldest first. The genesis CREATE is the first entry (prev = "")."""
    out: list[tuple[int, str, str]] = []
    cur = ""
    ce = _create_event(tk)
    if ce:
        new = str(ce.get("data", {}).get("description", ""))
        out.append((ce["ts"], "", new))
        cur = new
    for e in _evs(tk, "EDIT"):
        fields = _edit_fields(e)
        if "description" not in fields:
            continue
        new = str(fields["description"])
        out.append((e["ts"], cur, new))
        cur = new
    return out


def _classify_delta(prev: str, new: str) -> str:
    """Classify one description delta into the §5.2 taxonomy. Precedence is applied by
    the caller across a window's deltas; this returns the single delta's class.

    The semantically-loaded classes (premise-invalidated / scope-reduction /
    approach-change) are NOT fabricated by the miner — a genuine, non-AC prose change
    surfaces as ``substantive-unclassified`` for the adjudication pass to split by hand.
    """
    if _norm_checkboxes(prev).strip() == _norm_checkboxes(new).strip():
        return "none"  # identical, or a pure progress check-off (not a plan edit)
    low_prev, low_new = prev.lower(), new.lower()
    if "## acceptance criteria" in low_new and "## acceptance criteria" not in low_prev:
        return "plan-authored-post-claim"
    if "[operator-attested]" in low_new and "[operator-attested]" not in low_prev:
        return "operator-attested-retag"
    if _ac_block(prev) != _ac_block(new):
        return "ac-strengthened"
    # non-AC prose change; small edits are cosmetic, large ones need adjudication.
    if abs(len(new) - len(prev)) < 120:
        return "cosmetic"
    return "substantive-unclassified"


# Highest-precedence class wins across a window's deltas.
_CLASS_PRECEDENCE = [
    "plan-authored-post-claim",
    "operator-attested-retag",
    "ac-strengthened",
    "substantive-unclassified",
    "cosmetic",
    "none",
]


def post_claim_edit_class(tk: dict[str, list[dict[str, Any]]]) -> str:
    """Classify first-cycle post-claim description edits into the §5.2 taxonomy by
    DIFFING consecutive description states (so a routine checkbox check-off does not read
    as a plan change). Only the first claim->close cycle is classified; edits in any
    post-reopen window are excluded (reopen_count is recorded separately)."""
    claim_ts, close_ts = _claim_and_close_ts(tk)
    if claim_ts is None:
        return "none"  # no observable first in_progress — outside the §5.2 505-population
    classes = {
        _classify_delta(prev, new)
        for ts, prev, new in _description_timeline(tk)
        if ts >= claim_ts and (close_ts is None or ts <= close_ts)
    }
    for cls in _CLASS_PRECEDENCE:
        if cls in classes:
            return cls
    return "none"


# ── corpus assembly ──────────────────────────────────────────────────────────
def build_row(tid: str, tk: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    ttype = ticket_type(tk) or "unknown"
    rr = review_round_count(tk)
    return {
        "ticket_id": tid,
        "ticket_type": ttype,
        "level": LEVEL.get(ttype, ttype),
        "post_claim_edit_class": post_claim_edit_class(tk),
        "reopen_count": reopen_count(tk),
        "force_close": force_close(tk),
        "completion_verifier_fail_count": completion_verifier_fail_count(tk),
        "review_round_count": rr,
        "had_persisted_review": rr > 0,
    }


def build_corpus() -> list[dict[str, Any]]:
    store = load_events()
    reviewed = sorted(tid for tid, tk in store.items() if review_round_count(tk) > 0)
    if len(reviewed) < REVIEWED_FLOOR:
        sys.exit(
            f"tickets history incomplete: recovered {len(reviewed)} reviewed tickets "
            f"from git objects (< floor {REVIEWED_FLOOR}, expected ~527). gc/pruned "
            f"objects suspected — refusing to write a truncated corpus."
        )
    return [build_row(tid, store[tid]) for tid in reviewed]


def _atomic_write_jsonl(rows: list[dict[str, Any]], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ── §5 re-verification ───────────────────────────────────────────────────────
# The report's §5 published figures (docs/research/task-decomposition-sota-2026.md,
# commit 143d5074e). --verify-s5 re-derives each from the frozen corpus and prints
# derived-vs-published so the README table can mark AGREES / CORRECTED-TO.
S5_PUBLISHED = {
    "reviewed_tickets": 527,
    "work_tickets": 1256,
    "claimed_work_tickets": 505,
    "post_claim_edits": 16,
    "post_claim_edit_rate": "3.2%",
    "substantive_edits": 15,
    "missed_cases": ["dc58-af7b", "db7b-c8fd", "5886-d028"],
    "caught_but_ignored_cases": ["c8cc-68b8", "f5df-0069", "115b-ceea", "8c4f-b81c"],
    "unknowable_cases": ["3006-e198"],
}

# The §5.2 substantive vocabulary (everything that is a real, non-cosmetic post-claim edit).
_SUBSTANTIVE_CLASSES = {
    "plan-authored-post-claim",
    "premise-invalidated",
    "scope-reduction",
    "approach-change",
    "ac-strengthened",
    "operator-attested-retag",
    "substantive-unclassified",
}


_WORK_TYPES = {"task", "story", "bug"}


def verify_s5(rows: list[dict[str, Any]]) -> dict[str, Any]:
    reviewed = len(rows)
    edited = [r for r in rows if r["post_claim_edit_class"] != "none"]
    substantive = [r for r in edited if r["post_claim_edit_class"] in _SUBSTANTIVE_CLASSES]
    class_dist = Counter(r["post_claim_edit_class"] for r in rows)

    # The report's basis is WORK tickets (task/story/bug, not epic/session_log). Report
    # this subset too so the derived rate is comparable to §5's 16/505 modulo the
    # reviewed-vs-all-claimed-work population difference (documented in the README).
    work = [r for r in rows if r["ticket_type"] in _WORK_TYPES]
    work_edited = [r for r in work if r["post_claim_edit_class"] != "none"]

    def _rate(n: int, d: int) -> str:
        return f"{n}/{d} = {(100 * n / d):.1f}%" if d else "n/a"

    def _case(short: str) -> dict[str, Any]:
        # §5 pins short ids; the corpus keys full 4-quad ids. Match on prefix.
        hit = next((r for r in rows if r["ticket_id"].startswith(short)), None)
        return {
            "present": hit is not None,
            "post_claim_edit_class": hit["post_claim_edit_class"] if hit else None,
        }

    return {
        "reviewed_tickets": {
            "derived": reviewed,
            "published": S5_PUBLISHED["reviewed_tickets"],
        },
        "post_claim_edited_in_reviewed": {
            "derived": len(edited),
            "rate": _rate(len(edited), reviewed),
        },
        "post_claim_edited_reviewed_work_only": {
            "derived": len(work_edited),
            "rate": _rate(len(work_edited), len(work)),
            "basis": "reviewed work tickets (task/story/bug) — closest to §5's 505 basis",
        },
        "substantive_edits_in_reviewed": {
            "derived": len(substantive),
            "published_share": S5_PUBLISHED["substantive_edits"],
        },
        "post_claim_edit_class_distribution": dict(class_dist),
        "published_post_claim_edits": S5_PUBLISHED["post_claim_edits"],
        "published_rate": S5_PUBLISHED["post_claim_edit_rate"],
        "missed_cases": {c: _case(c) for c in S5_PUBLISHED["missed_cases"]},
        "caught_but_ignored_cases": {c: _case(c) for c in S5_PUBLISHED["caught_but_ignored_cases"]},
        "unknowable_cases": {c: _case(c) for c in S5_PUBLISHED["unknowable_cases"]},
        "note": (
            "post_claim edits here are counted over the reviewed population; §5's "
            "16/505 is over work tickets with an observable first in_progress. The README "
            "table records derived-vs-published per figure with AGREES / CORRECTED-TO."
        ),
    }


# ── main ─────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="compute + validate, write nothing")
    ap.add_argument("--verify-s5", action="store_true", help="print the §5 re-derivation table")
    args = ap.parse_args(argv)

    rows = build_corpus()

    if args.verify_s5:
        print(json.dumps(verify_s5(rows), indent=2, ensure_ascii=False))
        return 0
    if args.dry_run:
        print(f"[dry-run] {len(rows)} reviewed-ticket rows; no file written.")
        print(json.dumps(rows[0], ensure_ascii=False))
        return 0

    _atomic_write_jsonl(rows, OUT_PATH)
    print(f"wrote {len(rows)} rows -> {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
