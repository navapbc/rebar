"""Freeze a replayable plan-review corpus (ticket celestine-treasonous-ewe / 471c-f71f-342c-45ed).

Enumerates ``plan_review_result_v1``/``v2`` sidecars from a tickets-tracker git history
(the git-object-walk pattern proven in
``docs/experiments/plan-review-gate/harnesses/mine_outcome_corpus.py`` — path-only
parsing recovers compacted/deleted blobs that an on-disk scan would miss), reconstructs
the at-review ticket material by replaying CREATE/EDIT events up to each sidecar's
timestamp, and marks a row ``verified`` when the reconstructed material's fingerprint
matches the sidecar's stored ``material_fingerprint`` under the SAME generation ladder
:func:`rebar.llm.plan_review.attest._legacy_material_ok` uses.

Pure git + local hashing: no LLM call, no network, no import of
``rebar.llm.runner`` / ``rebar.llm.review_kernel`` / any provider client.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.pass1 import material_fingerprint
from rebar.reducer._processors import _file_impact_scope

_PATH_RE = re.compile(
    r"^(?:(?:[0-9a-f]{2})/)?"
    r"(?P<tid>[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4})/"
    r"(?P<ts>\d+)-(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-"
    r"(?P<type>[A-Z_]+)\.json(?:\.retired)?$"
)

_CONTENT_TYPES = {"CREATE", "EDIT", "FILE_IMPACT", "REVIEW_RESULT"}
_SCHEMAS = {"plan_review_result_v1", "plan_review_result_v2"}

# (label, kwargs) — the SAME order/candidates as attest._legacy_material_ok, plus the
# current (all-default) generation tried first.
_GENERATION_LADDER: tuple[tuple[str, dict[str, bool]], ...] = (
    ("current", {}),
    ("pre_330c", {"normalize_checkboxes": False, "normalize_reason": False}),
    ("post_330c_pre_2be7", {"normalize_whitespace": False, "normalize_reason": False}),
    ("post_2be7_pre_reason", {"normalize_reason": False}),
)


class _Blob:
    __slots__ = ("kind", "sha", "ticket_id", "ts", "uuid")

    def __init__(self, sha: str, ticket_id: str, ts: int, uuid: str, kind: str) -> None:
        self.sha = sha
        self.ticket_id = ticket_id
        self.ts = ts
        self.uuid = uuid
        self.kind = kind


def _git_stdout(tracker_path: str, *args: str) -> str:
    return subprocess.run(  # raw-git-ok: read-only (only ever called with `rev-list`)
        ["git", "-C", tracker_path, *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _enumerate_event_blobs(tracker_path: str) -> list[_Blob]:
    """``(sha, ticket_id, ts, uuid, TYPE)`` for every event blob in tracker history,
    deduped by ``(ticket_id, uuid)`` (a blob may appear at multiple commits, active
    and ``.retired``)."""
    out = _git_stdout(tracker_path, "rev-list", "--objects", "--all")
    seen: set[tuple[str, str]] = set()
    blobs: list[_Blob] = []
    for line in out.splitlines():
        sha, _, path = line.partition(" ")
        if not path:
            continue
        m = _PATH_RE.match(path)
        if not m:
            continue
        key = (m["tid"], m["uuid"])
        if key in seen:
            continue
        seen.add(key)
        blobs.append(_Blob(sha, m["tid"], int(m["ts"]), m["uuid"], m["type"]))
    return blobs


def _batch_cat(tracker_path: str, shas: list[str]) -> dict[str, bytes]:
    """Bulk-read many blobs via one ``git cat-file --batch`` call.

    stdin is fed from a background thread while the main thread drains stdout —
    writing every request before reading any reply deadlocks once git's output fills
    the OS pipe buffer (mirrors ``mine_outcome_corpus.py``'s ``_batch_cat``)."""
    if not shas:
        return {}
    proc = subprocess.Popen(
        ["git", "-C", tracker_path, "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout

    def _feed() -> None:
        try:
            proc.stdin.write(("\n".join(shas) + "\n").encode())  # type: ignore[union-attr]
        finally:
            proc.stdin.close()  # type: ignore[union-attr]

    writer = threading.Thread(target=_feed, daemon=True)
    writer.start()

    contents: dict[str, bytes] = {}
    for _ in shas:
        header = proc.stdout.readline().decode()
        parts = header.split()
        if len(parts) != 3:
            continue
        oid, _otype, size = parts[0], parts[1], int(parts[2])
        body = proc.stdout.read(size)
        proc.stdout.read(1)
        contents[oid] = body
    writer.join()
    proc.wait()
    return contents


def _load_ticket_events(tracker_path: str) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """``{ticket_id: {TYPE: [event, ...]}}``, each event ``{"uuid", "ts", "data"}``,
    sorted oldest-first. Only CREATE/EDIT/REVIEW_RESULT content is read."""
    blobs = _enumerate_event_blobs(tracker_path)
    content_shas = [b.sha for b in blobs if b.kind in _CONTENT_TYPES]
    bodies = _batch_cat(tracker_path, content_shas)

    store: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for b in blobs:
        if b.kind not in _CONTENT_TYPES:
            continue
        raw = bodies.get(b.sha)
        if raw is None:
            continue
        try:
            data = json.loads(raw).get("data", {})
        except (json.JSONDecodeError, AttributeError):
            data = {}
        by_type = store.setdefault(b.ticket_id, {})
        by_type.setdefault(b.kind, []).append({"uuid": b.uuid, "ts": b.ts, "data": data})
    for by_type in store.values():
        for events in by_type.values():
            events.sort(key=lambda e: e["ts"])
    return store


def _reconstruct_material(
    ticket_events: dict[str, list[dict[str, Any]]], review_ts: int
) -> tuple[str, str, list[Any], str | None, str | None, list[str], bool]:
    """Replay CREATE + EDITs up to ``review_ts`` (inclusive), oldest-first, into the
    reconstructed ``(ticket_type, description, file_impact, file_impact_scope,
    no_file_impact_reason, children, create_found)`` state as of the review.

    ``create_found=False`` (no recoverable CREATE event) is a RECONSTRUCTION FAILURE,
    distinct from a ticket that genuinely has an empty description — the caller must
    not attempt fingerprint matching against the resulting empty material, which would
    conflate "we can't tell" with "the material changed" (or worse, a spurious match)."""
    creates = ticket_events.get("CREATE", [])
    create_found = bool(creates)
    create_data = creates[0]["data"] if creates else {}
    ticket_type = create_data.get("ticket_type", "")
    description = create_data.get("description", "")
    file_impact: list[Any] = []
    file_impact_scope: str | None = None
    no_file_impact_reason: str | None = None

    for e in ticket_events.get("EDIT", []):
        if e["ts"] > review_ts:
            continue
        fields = e["data"].get("fields")
        if not isinstance(fields, dict):
            continue
        if "description" in fields:
            description = fields["description"]

    # file_impact / file_impact_scope / no_file_impact_reason are written EXCLUSIVELY by
    # FILE_IMPACT events (rebar._commands.leaf.set_file_impact), never by EDIT's `fields` —
    # mirror the reducer's own LWW replace (rebar.reducer._processors.process_file_impact /
    # _file_impact_scope) rather than reading them off EDIT.
    for e in ticket_events.get("FILE_IMPACT", []):
        if e["ts"] > review_ts:
            continue
        data = e["data"]
        file_impact = data.get("file_impact") or []
        file_impact_scope, no_file_impact_reason = _file_impact_scope(data, file_impact)

    return (
        ticket_type,
        description,
        file_impact,
        file_impact_scope,
        no_file_impact_reason,
        [],
        create_found,
    )


def _build_context(
    ticket_id: str,
    ticket_type: str,
    description: str,
    file_impact: list[Any],
    file_impact_scope: str | None,
    no_file_impact_reason: str | None,
    children: list[str],
) -> PlanContext:
    state: dict[str, Any] = {"file_impact": file_impact}
    if file_impact_scope == "none":
        state["file_impact_scope"] = "none"
        state["no_file_impact_reason"] = no_file_impact_reason
    return PlanContext(
        ticket_id=ticket_id,
        ticket_type=ticket_type,
        title="",
        description=description,
        state=state,
        children=[{"ticket_id": c} for c in children],
    )


def _match_generation(ctx: PlanContext, signed_fingerprint: str) -> str | None:
    for label, kwargs in _GENERATION_LADDER:
        if material_fingerprint(ctx, **kwargs) == signed_fingerprint:
            return label
    return None


def _child_ids(reviewed_related_material: Any) -> list[str]:
    if not isinstance(reviewed_related_material, list):
        return []
    return [
        item["canonical_id"]
        for item in reviewed_related_material
        if isinstance(item, dict) and item.get("role") == "child" and item.get("canonical_id")
    ]


def _build_sidecar_row(
    store_name: str,
    ticket_id: str,
    ticket_events: dict[str, list[dict[str, Any]]],
    review_event: dict[str, Any],
) -> dict[str, Any]:
    data = review_event["data"]
    review_ts = review_event["ts"]

    ttype, description, file_impact, scope, reason, _, create_found = _reconstruct_material(
        ticket_events, review_ts
    )
    children = _child_ids(data.get("reviewed_related_material"))
    signed_fingerprint = data.get("material_fingerprint", "")

    if create_found:
        ctx = _build_context(ticket_id, ttype, description, file_impact, scope, reason, children)
        generation = _match_generation(ctx, signed_fingerprint)
    else:
        # No recoverable CREATE event: reconstruction failed, not "the material is
        # empty" — never attempt a fingerprint match against fabricated empty material.
        generation = None

    return {
        "store": store_name,
        "ticket_id": ticket_id,
        "review_event_ts": review_ts,
        "review_event_uuid": review_event["uuid"],
        "schema": data.get("schema"),
        "verdict": data.get("verdict"),
        "ticket_type": ttype,
        "description": description,
        "file_impact": file_impact,
        "file_impact_scope": scope,
        "no_file_impact_reason": reason,
        "children": children,
        "material_fingerprint": signed_fingerprint,
        "verified": generation is not None,
        "generation": generation,
        "reconstructed": create_found,
        "ran_model": (data.get("provider_provenance") or {}).get("ran_model"),
    }


def _rows_for_store(store_name: str, tracker_path: str) -> list[dict[str, Any]]:
    events = _load_ticket_events(tracker_path)
    rows: list[dict[str, Any]] = []
    for ticket_id, by_type in events.items():
        for review_event in by_type.get("REVIEW_RESULT", []):
            if review_event["data"].get("schema") not in _SCHEMAS:
                continue
            rows.append(_build_sidecar_row(store_name, ticket_id, by_type, review_event))
    return rows


def _sort_key(row: dict[str, Any]) -> tuple[str, str, int]:
    return (row["store"], row["ticket_id"], row["review_event_ts"])


def _compute_manifest(rows: list[dict[str, Any]], store_names: list[str]) -> dict[str, Any]:
    stores: dict[str, int] = {name: 0 for name in store_names}
    schema_histogram: dict[str, int] = {s: 0 for s in _SCHEMAS}
    verdict_histogram: dict[str, int] = {}
    verified_by_generation: dict[str, int] = {label: 0 for label, _ in _GENERATION_LADDER}
    verified_count = 0
    unverified_count = 0

    for row in rows:
        stores[row["store"]] = stores.get(row["store"], 0) + 1
        schema_histogram[row["schema"]] = schema_histogram.get(row["schema"], 0) + 1
        verdict = row["verdict"]
        verdict_histogram[verdict] = verdict_histogram.get(verdict, 0) + 1
        if row["verified"]:
            verified_count += 1
            verified_by_generation[row["generation"]] += 1
        else:
            unverified_count += 1

    total = verified_count + unverified_count
    verified_ratio = verified_count / total if total else 0.0

    payload = json.dumps(
        [{k: v for k, v in row.items()} for row in sorted(rows, key=_sort_key)],
        sort_keys=True,
        ensure_ascii=False,
    )
    content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    return {
        "stores": stores,
        "schema_histogram": schema_histogram,
        "verdict_histogram": verdict_histogram,
        "verified_count": verified_count,
        "unverified_count": unverified_count,
        "verified_ratio": verified_ratio,
        "verified_by_generation": verified_by_generation,
        "row_count": len(rows),
        "content_hash": content_hash,
    }


def _write_cache(rows: list[dict[str, Any]], cache_dir: Path, content_hash: str) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_path = cache_dir / f"{content_hash}.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        for row in sorted(rows, key=_sort_key):
            if not row["verified"]:
                continue
            fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")


def build_corpus(store_roots: dict[str, str], *, cache_dir: Path | str) -> dict[str, Any]:
    """Enumerate ``plan_review_result_v1``/``v2`` sidecars from every store in
    ``store_roots`` (``{store_name: tracker_git_root}``), reconstruct each sidecar's
    at-review material from git history, and verify its stored ``material_fingerprint``
    against the reconstruction under the legacy generation ladder.

    Writes a compact JSONL corpus (verified rows only) into ``cache_dir``, keyed by the
    manifest's ``content_hash``, and returns the manifest.
    """
    rows: list[dict[str, Any]] = []
    for store_name, tracker_path in store_roots.items():
        rows.extend(_rows_for_store(store_name, tracker_path))

    manifest = _compute_manifest(rows, list(store_roots))
    _write_cache(rows, Path(cache_dir), manifest["content_hash"])
    return manifest
