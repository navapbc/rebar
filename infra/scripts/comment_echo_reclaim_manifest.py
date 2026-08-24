#!/usr/bin/env python3
"""Build the immutable incident manifest for the reconciler comment-echo reclaim.

The builder is deliberately read-only with respect to both Git and Jira.  It consumes
a complete, paginated Jira inventory captured by an operator, verifies the independently
restored backup produced by ``prepare_reclaim_backup.py``, and writes one off-repository
manifest.  It never creates a Git ref, updates a remote, or guesses a survivor by age.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from prepare_reclaim_backup import _bundle_heads, _parse_remote_refs, _remote_snapshot
from reclaim_bridge_history import ReclaimError, is_partial_clone, rev_parse, run_git

from rebar._engine import engine_dir

_INBOUND_FIELDS_PATH = engine_dir() / "rebar_reconciler" / "inbound_fields.py"
_INBOUND_FIELDS_SPEC = importlib.util.spec_from_file_location(
    "_comment_echo_reclaim_inbound_fields", _INBOUND_FIELDS_PATH
)
if _INBOUND_FIELDS_SPEC is None or _INBOUND_FIELDS_SPEC.loader is None:
    raise ImportError(f"cannot load production rich-text normalizer: {_INBOUND_FIELDS_PATH}")
_INBOUND_FIELDS = importlib.util.module_from_spec(_INBOUND_FIELDS_SPEC)
sys.modules[_INBOUND_FIELDS_SPEC.name] = _INBOUND_FIELDS
_INBOUND_FIELDS_SPEC.loader.exec_module(_INBOUND_FIELDS)
normalize_rich_text = _INBOUND_FIELDS.normalize_rich_text

SCHEMA_VERSION = 1
UNKNOWN_COMMENT_ID = "__rebar_unknown_comment_id__"
INCIDENT_GROUPS: dict[str, tuple[str, ...]] = {
    "8625-7bea-67db-4cdc": (
        "2a28832f8d7fdb3bbadf4557d163037c31fb6bc32693965c42aa18629ef7de5e",
        "c39cb4843535b2c26790137988010500e53d3dcce3098d315d6ea20f7e00c7e4",
        "eacbefb8d54080f1217c70dc4be4641d20a25b32f88fb42ff5b2a2efb2e64dab",
    ),
    "9305-b42c-3262-4c58": (
        "d429bb0c7d590613d15e2f3f54ed0366143760ca14df0412d14d9bbcaeff6ada",
        "fe854c5c4a276fd5a1a92132c3a9598d3828bfa687d6a23fd75a8afc06e0397b",
    ),
    "e27c-5a20-cd13-43b2": ("45f2497fe6298f1172575407f534a63a2dc0825b54c51f29b40b1470f6a73735",),
}
_INCIDENT_BOT_ACCOUNT_ID = "712020:6471376f-4e5e-4ed2-8c05-330827bc387e"
_INCIDENT_CLEANUP_ROWS = (
    (
        "8625-7bea-67db-4cdc",
        "eacbefb8d54080f1217c70dc4be4641d20a25b32f88fb42ff5b2a2efb2e64dab",
        "REB-1567",
        "963510",
        "997143",
    ),
    (
        "8625-7bea-67db-4cdc",
        "2a28832f8d7fdb3bbadf4557d163037c31fb6bc32693965c42aa18629ef7de5e",
        "REB-1567",
        "963511",
        "997144",
    ),
    (
        "8625-7bea-67db-4cdc",
        "c39cb4843535b2c26790137988010500e53d3dcce3098d315d6ea20f7e00c7e4",
        "REB-1567",
        "963512",
        "997145",
    ),
    (
        "9305-b42c-3262-4c58",
        "fe854c5c4a276fd5a1a92132c3a9598d3828bfa687d6a23fd75a8afc06e0397b",
        "REB-1931",
        "963788",
        "997255",
    ),
    (
        "9305-b42c-3262-4c58",
        "d429bb0c7d590613d15e2f3f54ed0366143760ca14df0412d14d9bbcaeff6ada",
        "REB-1931",
        "963789",
        "997256",
    ),
    (
        "e27c-5a20-cd13-43b2",
        "45f2497fe6298f1172575407f534a63a2dc0825b54c51f29b40b1470f6a73735",
        "REB-2605",
        "964272",
        "997293",
    ),
)
INCIDENT_CLEANUP_AUTHORITY = {
    (ticket, body_digest): {
        "jira_key": jira_key,
        "survivor_id": survivor_id,
        "delete_id": delete_id,
        "author_account_id": _INCIDENT_BOT_ACCOUNT_ID,
        "post_run_id": "32447039101",
        "import_run_id": "32447697242",
        "import_commit": "ef716a48cee23bafe155ed7cb256ccb49bf316e0",
    }
    for ticket, body_digest, jira_key, survivor_id, delete_id in _INCIDENT_CLEANUP_ROWS
}


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


_canonical = canonical_json


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _open_regular(path: Path, label: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReclaimError(f"{label} must be a regular file: {path}") from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ReclaimError(f"{label} must be a regular file: {path}")
    return descriptor


def _read_regular(path: Path, label: str) -> bytes:
    descriptor = _open_regular(path, label)
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


def _sha256_file(path: Path, label: str) -> str:
    digest = hashlib.sha256()
    descriptor = _open_regular(path, label)
    with os.fdopen(descriptor, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _blob_oid(repo: Path, raw: bytes) -> str:
    return run_git(repo, ["hash-object", "--stdin"], input_bytes=raw).stdout.decode().strip()


def _load_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = _read_regular(path, label)
        value = json.loads(raw)
    except ValueError as exc:
        raise ReclaimError(f"{label} is unreadable or invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ReclaimError(f"{label} must contain a JSON object: {path}")
    return value, raw


def _assert_regular_off_repo(repo: Path, path: Path, label: str) -> Path:
    if path.absolute().is_symlink():
        raise ReclaimError(f"{label} must not be a symlink: {path.absolute()}")
    resolved = path.resolve()
    try:
        resolved.relative_to(repo)
    except ValueError:
        pass
    else:
        raise ReclaimError(f"{label} must be outside the source repository: {resolved}")
    try:
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise ReclaimError(f"{label} does not exist or is unreadable: {resolved}") from exc
    if not stat.S_ISREG(mode):
        raise ReclaimError(f"{label} must be a regular file: {resolved}")
    return resolved


def _assert_new_off_repo(repo: Path, path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(repo)
    except ValueError:
        pass
    else:
        raise ReclaimError(f"manifest output must be outside the source repository: {resolved}")
    if resolved.exists() or resolved.is_symlink():
        raise ReclaimError(f"refusing to overwrite manifest output: {resolved}")
    return resolved


def _assert_source(repo: Path, source_ref: str, source_tip: str) -> str:
    if not repo.is_dir() or not (repo / ".git").is_dir():
        raise ReclaimError("source must be a standalone Git worktree, not a linked worktree")
    if run_git(repo, ["status", "--porcelain"]).stdout:
        raise ReclaimError("source worktree is not clean")
    if run_git(repo, ["rev-parse", "--is-shallow-repository"]).stdout.strip() == b"true":
        raise ReclaimError("shallow source is unsafe; use a fresh unfiltered clone")
    if is_partial_clone(repo):
        raise ReclaimError("partial/promisor source is unsafe; use a fresh unfiltered clone")
    alternates = Path(
        run_git(repo, ["rev-parse", "--git-path", "objects/info/alternates"])
        .stdout.decode()
        .strip()
    )
    if not alternates.is_absolute():
        alternates = repo / alternates
    if alternates.exists() and alternates.read_text(encoding="utf-8").strip():
        raise ReclaimError("alternate object databases are unsafe; use a fresh unfiltered clone")
    resolved = rev_parse(repo, source_ref)
    if resolved != source_tip:
        raise ReclaimError("--source-ref does not resolve to the exact --source-tip")
    return resolved


def _tree(repo: Path, tip: str) -> tuple[dict[str, str], dict[str, list[str]]]:
    raw = run_git(
        repo,
        ["ls-tree", "-r", "-z", "--format=%(objectname)%x09%(path)", tip],
    ).stdout
    paths: dict[str, str] = {}
    by_blob: dict[str, list[str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            oid_raw, path_raw = record.split(b"\t", 1)
        except ValueError as exc:
            raise ReclaimError("malformed git ls-tree output") from exc
        oid = oid_raw.decode("ascii")
        path = path_raw.decode("utf-8", "surrogateescape")
        paths[path] = oid
        by_blob.setdefault(oid, []).append(path)
    return paths, by_blob


def _read_blob(repo: Path, oid: str) -> bytes:
    return run_git(repo, ["cat-file", "blob", oid]).stdout


def _aliases(path: str) -> list[str]:
    active = path.removesuffix(".retired")
    return sorted({active, f"{active}.retired"})


def _event_identity(path: str, event: Mapping[str, Any]) -> None:
    name = Path(path).name.removesuffix(".retired")
    expected = f"{event.get('timestamp')}-{event.get('uuid')}-COMMENT.json"
    if name != expected:
        raise ReclaimError(f"COMMENT envelope identity differs from its path: {path}")


def _candidate_events(
    repo: Path,
    paths: Mapping[str, str],
    groups: Mapping[str, Sequence[str]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    found = {(ticket, digest): [] for ticket, digests in groups.items() for digest in digests}
    seen_uuids: set[str] = set()
    for path, oid in sorted(paths.items()):
        ticket, separator, name = path.partition("/")
        if not separator or ticket not in groups or "-COMMENT.json" not in name:
            continue
        raw = _read_blob(repo, oid)
        try:
            event = json.loads(raw)
        except ValueError as exc:
            raise ReclaimError(f"invalid COMMENT JSON at {path}") from exc
        body = event.get("data", {}).get("body") if isinstance(event, dict) else None
        if not isinstance(body, str):
            continue
        body_digest = _sha256(body.encode("utf-8"))
        if body_digest not in groups[ticket]:
            continue
        author_match = event.get("author") == "reconciler"
        env_match = event.get("env_id") == "reconciler"
        if not author_match and not env_match:
            continue  # The original local source comment is not an echo candidate.
        if not author_match or not env_match or event.get("event_type") != "COMMENT":
            raise ReclaimError(f"target body has an ambiguous reconciler envelope: {path}")
        jira_id = event.get("data", {}).get("jira_comment_id")
        if not isinstance(jira_id, str) or not jira_id or jira_id == UNKNOWN_COMMENT_ID:
            raise ReclaimError(f"target reconciler COMMENT lacks a real Jira id: {path}")
        uuid = event.get("uuid")
        timestamp = event.get("timestamp")
        if not isinstance(uuid, str) or not isinstance(timestamp, int) or uuid in seen_uuids:
            raise ReclaimError(f"target COMMENT has invalid or duplicate identity: {path}")
        _event_identity(path, event)
        seen_uuids.add(uuid)
        found[(ticket, body_digest)].append(
            {
                "path": path,
                "path_aliases": _aliases(path),
                "blob_oid": oid,
                "blob_sha256": _sha256(raw),
                "bytes": len(raw),
                "event_uuid": uuid,
                "timestamp": timestamp,
                "jira_comment_id": jira_id,
                "signed": bool(event.get("author_sig")),
                "comment": {
                    "body": body,
                    "author": event.get("author"),
                    "timestamp": timestamp,
                    **(
                        {"author_email": event["author_email"]}
                        if event.get("author_email") is not None
                        else {}
                    ),
                    **(
                        {"author_id": event["author_id"]}
                        if event.get("author_id") is not None
                        else {}
                    ),
                    "jira_comment_id": jira_id,
                    **{
                        key: event["data"][key]
                        for key in ("source_author", "source_created_at")
                        if event.get("data", {}).get(key) is not None
                    },
                },
            }
        )
    missing = [f"{ticket}/{digest}" for (ticket, digest), events in found.items() if not events]
    if missing:
        raise ReclaimError(f"incident group has no local reconciler COMMENT: {missing[0]}")
    return found


def inventory_by_key(inventory: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    if inventory.get("schema_version") != 1 or inventory.get("source") != "jira-cloud-rest-v3":
        raise ReclaimError("Jira inventory has an unsupported schema or source")
    issues = inventory.get("issues")
    if not isinstance(issues, list):
        raise ReclaimError("Jira inventory issues must be a list")
    result: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("key"), str):
            raise ReclaimError("Jira inventory contains a malformed issue")
        key = issue["key"]
        if key in result or not isinstance(issue.get("pages"), list) or not issue["pages"]:
            raise ReclaimError(f"Jira inventory has duplicate or absent pages for {key}")
        comments: list[dict[str, Any]] = []
        expected_start = 0
        total: int | None = None
        seen_ids: set[str] = set()
        for page in issue["pages"]:
            if not isinstance(page, dict):
                raise ReclaimError(f"Jira inventory page is malformed for {key}")
            page_comments = page.get("comments")
            start = page.get("startAt")
            maximum = page.get("maxResults")
            page_total = page.get("total")
            if (
                not isinstance(page_comments, list)
                or not isinstance(start, int)
                or not isinstance(maximum, int)
                or maximum <= 0
                or not isinstance(page_total, int)
                or page_total < 0
                or start != expected_start
                or len(page_comments) > maximum
                or (total is not None and page_total != total)
            ):
                raise ReclaimError(f"Jira pagination is incomplete or inconsistent for {key}")
            total = page_total
            for comment in page_comments:
                if not isinstance(comment, dict):
                    raise ReclaimError(f"Jira inventory comment is malformed for {key}")
                comment_id = comment.get("id")
                body = comment.get("body")
                author_object = comment.get("author")
                author = author_object.get("accountId") if isinstance(author_object, dict) else None
                if (
                    not isinstance(comment_id, str)
                    or not comment_id
                    or comment_id in seen_ids
                    or not isinstance(body, dict)
                    or body.get("type") != "doc"
                    or body.get("version") != 1
                    or not isinstance(body.get("content"), list)
                    or not isinstance(author, str)
                    or not author
                ):
                    raise ReclaimError(f"Jira inventory comment identity is invalid for {key}")
                try:
                    normalized_body = normalize_rich_text(body)
                except (AttributeError, TypeError, ValueError) as exc:
                    raise ReclaimError(f"Jira inventory comment body is invalid for {key}") from exc
                seen_ids.add(comment_id)
                normalized_comment = copy.deepcopy(comment)
                normalized_comment["author_account_id"] = author
                normalized_comment["normalized_body"] = normalized_body
                comments.append(normalized_comment)
            expected_start += len(page_comments)
        if total is None or expected_start != total:
            raise ReclaimError(f"Jira inventory did not fetch the reported total for {key}")
        result[key] = comments
    return result


_inventory_by_key = inventory_by_key


def _bridge_constraints(
    bridge: Mapping[str, Any], groups: Mapping[str, Sequence[str]]
) -> tuple[dict[str, str], dict[str, str]]:
    bindings = bridge.get("bindings")
    reverse = bridge.get("reverse")
    comment_ids = bridge.get("comment_ids")
    if (
        not isinstance(bindings, dict)
        or not isinstance(reverse, dict)
        or not isinstance(comment_ids, dict)
    ):
        raise ReclaimError("bridge bindings file has an unsupported shape")
    jira_keys: dict[str, str] = {}
    for ticket in groups:
        binding = bindings.get(ticket)
        if not isinstance(binding, dict) or binding.get("state") != "confirmed":
            raise ReclaimError(f"target ticket lacks a confirmed Jira binding: {ticket}")
        key = binding.get("jira_key")
        if not isinstance(key, str) or not key or reverse.get(key) != ticket:
            raise ReclaimError(f"target Jira binding/reverse map disagrees for {ticket}")
        jira_keys[ticket] = key
    if not all(
        isinstance(key, str) and isinstance(value, str) for key, value in comment_ids.items()
    ):
        raise ReclaimError("bridge comment_ids must map string positions to string ids")
    return jira_keys, dict(comment_ids)


def _select_groups(
    candidates: Mapping[tuple[str, str], list[dict[str, Any]]],
    groups: Mapping[str, Sequence[str]],
    jira_keys: Mapping[str, str],
    comment_ids: Mapping[str, str],
    inventory: Mapping[str, list[dict[str, Any]]],
    cleanup_authority: Mapping[tuple[str, str], Mapping[str, str]],
) -> list[dict[str, Any]]:
    expected_keys = set(jira_keys.values())
    if set(inventory) != expected_keys:
        raise ReclaimError("Jira inventory must contain exactly the three bound target issues")
    real_mapped_ids = {value for value in comment_ids.values() if value != UNKNOWN_COMMENT_ID}
    selected: list[dict[str, Any]] = []
    for ticket, digests in groups.items():
        jira_key = jira_keys[ticket]
        live_comments = inventory[jira_key]
        for body_digest in digests:
            authority = cleanup_authority.get((ticket, body_digest))
            required = {
                "jira_key",
                "survivor_id",
                "delete_id",
                "author_account_id",
                "post_run_id",
                "import_run_id",
                "import_commit",
            }
            if (
                not isinstance(authority, Mapping)
                or set(authority) != required
                or not all(isinstance(authority[key], str) and authority[key] for key in required)
                or authority["jira_key"] != jira_key
                or authority["survivor_id"] == authority["delete_id"]
            ):
                raise ReclaimError(
                    f"incident cleanup authority is invalid for {ticket}/{body_digest}"
                )
            live = [
                item
                for item in live_comments
                if _sha256(item["normalized_body"].encode("utf-8")) == body_digest
            ]
            by_id = {item["id"]: item for item in live}
            expected_ids = {authority["survivor_id"], authority["delete_id"]}
            if len(live) != 2 or set(by_id) != expected_ids:
                raise ReclaimError(
                    f"Jira must contain exactly the authorized live pair for {ticket}/{body_digest}"
                )
            if any(item["author_account_id"] != authority["author_account_id"] for item in live):
                raise ReclaimError(f"Jira cleanup author differs for {ticket}/{body_digest}")
            live_id = authority["survivor_id"]
            local = candidates[(ticket, body_digest)]
            live_local = [item for item in local if item["jira_comment_id"] == live_id]
            if not live_local:
                raise ReclaimError(f"live Jira id has no matching local event: {ticket}/{live_id}")
            mapped = {
                item["jira_comment_id"]
                for item in local
                if item["jira_comment_id"] in real_mapped_ids
            }
            if mapped and mapped != {live_id}:
                raise ReclaimError(f"bridge mapped Jira id conflicts with live Jira for {ticket}")
            protected = [item for item in local if str(item["timestamp"]) in comment_ids]
            if len(protected) > 1 or (protected and protected[0]["jira_comment_id"] != live_id):
                raise ReclaimError(f"bridge comment_ids protection is ambiguous for {ticket}")
            survivor = (
                protected[0]
                if protected
                else sorted(live_local, key=lambda item: (item["path"], item["event_uuid"]))[0]
            )
            removed = [item for item in local if item["event_uuid"] != survivor["event_uuid"]]
            selected.append(
                {
                    "ticket_id": ticket,
                    "jira_key": jira_key,
                    "body_sha256": body_digest,
                    "live_jira_comment": copy.deepcopy(by_id[live_id]),
                    "jira_cleanup": {
                        "delete": copy.deepcopy(by_id[authority["delete_id"]]),
                        "survivor": copy.deepcopy(by_id[live_id]),
                        "authority": dict(authority),
                    },
                    "survivor": survivor,
                    "removed": removed,
                    "candidate_count": len(local),
                    "selection": (
                        "bridge-comment-position-and-live-jira-id"
                        if protected
                        else "live-jira-id-within-identity-lexicographic-tiebreak"
                    ),
                }
            )
    return selected


def _transform_snapshot(
    raw: bytes, removed: Sequence[dict[str, Any]]
) -> tuple[bytes, dict[str, Any]] | None:
    try:
        snapshot = json.loads(raw)
        data = snapshot["data"]
        state = data["compiled_state"]
        sources = data["source_event_uuids"]
        comments = state["comments"]
        ledger = state["authorship_ledger"]
        counts = state["authorship"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ReclaimError("target SNAPSHOT has an unsupported shape") from exc
    if _canonical(snapshot) != raw or snapshot.get("author_sig"):
        raise ReclaimError("target SNAPSHOT is non-canonical or carries a signature")
    if not all(isinstance(value, list) for value in (sources, comments, ledger)) or not isinstance(
        counts, dict
    ):
        raise ReclaimError("target SNAPSHOT list fields are malformed")
    if not all(isinstance(value, str) and value for value in sources) or len(sources) != len(
        set(sources)
    ):
        raise ReclaimError("target SNAPSHOT has duplicate or invalid source event UUIDs")
    if not all(isinstance(entry, dict) for entry in comments):
        raise ReclaimError("target SNAPSHOT comments are malformed")
    ledger_event_ids = [entry.get("event_uuid") for entry in ledger if isinstance(entry, dict)]
    if (
        len(ledger_event_ids) != len(ledger)
        or not all(isinstance(value, str) and value for value in ledger_event_ids)
        or len(ledger_event_ids) != len(set(ledger_event_ids))
    ):
        raise ReclaimError("target SNAPSHOT has duplicate or invalid authorship provenance")
    by_timestamp = {item["timestamp"]: item for item in removed}
    source_ids = set(sources)
    ledger_ids = set(ledger_event_ids)
    comment_hits = [entry for entry in comments if entry.get("timestamp") in by_timestamp]
    represented = {entry.get("timestamp") for entry in comment_hits}
    relevant = [
        item
        for item in removed
        if item["event_uuid"] in source_ids
        or item["event_uuid"] in ledger_ids
        or item["timestamp"] in represented
    ]
    if not relevant:
        return None
    for item in relevant:
        timestamp_hits = [
            entry for entry in comments if entry.get("timestamp") == item["timestamp"]
        ]
        source_member = item["event_uuid"] in source_ids
        ledger_member = item["event_uuid"] in ledger_ids
        if (
            len(timestamp_hits) != 1
            or timestamp_hits[0] != item["comment"]
            or ledger_member != (item["signed"] and source_member)
        ):
            raise ReclaimError("SNAPSHOT comment/source/authorship representation drifted")
    remove_ids = {item["event_uuid"] for item in relevant if item["event_uuid"] in source_ids}
    remove_timestamps = {item["timestamp"] for item in relevant}
    transformed = copy.deepcopy(snapshot)
    new_data = transformed["data"]
    new_state = new_data["compiled_state"]
    new_data["source_event_uuids"] = [uuid for uuid in sources if uuid not in remove_ids]
    new_state["comments"] = [
        entry for entry in comments if entry.get("timestamp") not in remove_timestamps
    ]
    new_state["authorship_ledger"] = [
        entry for entry in ledger if entry.get("event_uuid") not in remove_ids
    ]
    for signed in (True, False):
        bucket = "signed" if signed else "unsigned"
        decrement = sum(item["signed"] is signed for item in relevant)
        current = counts.get(bucket)
        if not isinstance(current, int) or current < decrement:
            raise ReclaimError("SNAPSHOT authorship counts cannot represent the removal")
        new_state["authorship"][bucket] = current - decrement
    return _canonical(transformed), {
        "remove_event_uuids": sorted(remove_ids),
        "remove_comment_timestamps": sorted(remove_timestamps),
    }


def _snapshot_transforms(
    repo: Path,
    paths: Mapping[str, str],
    groups: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    removed_by_ticket: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        removed_by_ticket.setdefault(group["ticket_id"], []).extend(group["removed"])
    transforms: list[dict[str, Any]] = []
    for path, oid in sorted(paths.items()):
        ticket, separator, name = path.partition("/")
        if not separator or ticket not in removed_by_ticket or "-SNAPSHOT.json" not in name:
            continue
        old = _read_blob(repo, oid)
        result = _transform_snapshot(old, removed_by_ticket[ticket])
        if result is None:
            continue
        new, delta = result
        transforms.append(
            {
                "ticket_id": ticket,
                "path": path,
                "path_aliases": _aliases(path),
                "old_blob_oid": oid,
                "new_blob_oid": _blob_oid(repo, new),
                "old_sha256": _sha256(old),
                "new_sha256": _sha256(new),
                "old_bytes": len(old),
                "new_bytes": len(new),
                **delta,
            }
        )
    return transforms


def _assert_unshared(
    by_blob: Mapping[str, list[str]],
    groups: Sequence[dict[str, Any]],
    snapshots: Sequence[dict[str, Any]],
) -> None:
    materials = [item for group in groups for item in [group["survivor"], *group["removed"]]]
    for item in materials:
        if by_blob.get(item["blob_oid"], []) != [item["path"]]:
            raise ReclaimError(
                f"target COMMENT blob is shared outside its exact path: {item['path']}"
            )
    for item in snapshots:
        if by_blob.get(item["old_blob_oid"], []) != [item["path"]]:
            raise ReclaimError(
                f"target SNAPSHOT blob is shared outside its exact path: {item['path']}"
            )


def _validate_backup(
    repo: Path,
    bundle: Path,
    backup: Mapping[str, Any],
    source_tip: str,
    remote_snapshot: bytes,
) -> None:
    if backup.get("schema_version") != 1 or backup.get("old_tip") != source_tip:
        raise ReclaimError("backup manifest does not bind the exact source tip")
    bundle_refs = backup.get("bundle_refs")
    if not isinstance(bundle_refs, dict) or not bundle_refs:
        raise ReclaimError("backup manifest has no bundle refs")
    run_git(repo, ["bundle", "verify", str(bundle)])
    if _bundle_heads(repo, bundle) != bundle_refs:
        raise ReclaimError("backup bundle heads differ from the backup manifest")
    old_tip_refs = [oid for ref, oid in bundle_refs.items() if ref.endswith("/old-tip")]
    if old_tip_refs != [source_tip]:
        raise ReclaimError("backup bundle does not contain exactly one source-tip restore ref")
    current = {
        ref.name: (ref.direct_oid, ref.peeled_oid) for ref in _parse_remote_refs(remote_snapshot)
    }
    recorded = backup.get("remote_refs")
    if not isinstance(recorded, list) or not recorded:
        raise ReclaimError("backup manifest has no remote-ref census")
    expected: dict[str, tuple[str, str | None]] = {}
    for item in recorded:
        if not isinstance(item, dict):
            raise ReclaimError("backup remote-ref census contains a malformed row")
        ref = item.get("ref")
        direct = item.get("direct_oid")
        peeled = item.get("peeled_oid")
        if (
            not isinstance(ref, str)
            or not ref.startswith(("refs/heads/", "refs/tags/"))
            or ref in expected
            or not isinstance(direct, str)
            or (peeled is not None and not isinstance(peeled, str))
        ):
            raise ReclaimError("backup remote-ref census contains an ambiguous row")
        expected[ref] = (direct, peeled)
    if current != expected:
        raise ReclaimError("live remote heads/tags differ from the verified backup census")


def _compat_transform(repo: Path, oid: str, epoch: str) -> dict[str, Any]:
    old = _read_blob(repo, oid)
    try:
        record = json.loads(old)
    except ValueError as exc:
        raise ReclaimError("source .store-compat.json is invalid") from exc
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("format_version"), int)
        or not isinstance(record.get("required_capabilities"), list)
    ):
        raise ReclaimError("source .store-compat.json has an unsupported shape")
    if record.get("epoch") == epoch:
        raise ReclaimError("derived reclaim epoch is not fresh")
    transformed = copy.deepcopy(record)
    transformed["epoch"] = epoch
    new = json.dumps(transformed, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    return {
        "path": ".store-compat.json",
        "old_blob_oid": oid,
        "new_blob_oid": _blob_oid(repo, new),
        "old_sha256": _sha256(old),
        "new_sha256": _sha256(new),
        "old_bytes": len(old),
        "new_bytes": len(new),
        "epoch": epoch,
    }


def build_manifest(
    args: argparse.Namespace,
    *,
    incident_groups: Mapping[str, Sequence[str]] = INCIDENT_GROUPS,
    cleanup_authority: Mapping[tuple[str, str], Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    repo = args.repo.resolve()
    source_tip = _assert_source(repo, args.source_ref, args.source_tip)
    bundle = _assert_regular_off_repo(repo, args.backup_bundle, "backup bundle")
    backup_path = _assert_regular_off_repo(repo, args.backup_manifest, "backup manifest")
    inventory_path = _assert_regular_off_repo(repo, args.jira_inventory, "Jira inventory")
    output = _assert_new_off_repo(repo, args.output)
    backup, backup_raw = _load_object(backup_path, "backup manifest")
    inventory_raw_object, inventory_raw = _load_object(inventory_path, "Jira inventory")
    initial_remote = _remote_snapshot(repo)
    _validate_backup(repo, bundle, backup, source_tip, initial_remote)
    paths, by_blob = _tree(repo, source_tip)
    bridge_oid = paths.get(".bridge_state/bindings.json")
    compat_oid = paths.get(".store-compat.json")
    if bridge_oid is None or compat_oid is None:
        raise ReclaimError("source tip lacks bridge bindings or store compatibility state")
    bridge_raw = _read_blob(repo, bridge_oid)
    try:
        bridge = json.loads(bridge_raw)
    except ValueError as exc:
        raise ReclaimError("source bridge bindings are invalid JSON") from exc
    jira_keys, comment_ids = _bridge_constraints(bridge, incident_groups)
    inventory = _inventory_by_key(inventory_raw_object)
    candidates = _candidate_events(repo, paths, incident_groups)
    effective_cleanup = (
        INCIDENT_CLEANUP_AUTHORITY if cleanup_authority is None else cleanup_authority
    )
    selected = _select_groups(
        candidates,
        incident_groups,
        jira_keys,
        comment_ids,
        inventory,
        effective_cleanup,
    )
    snapshots = _snapshot_transforms(repo, paths, selected)
    _assert_unshared(by_blob, selected, snapshots)
    removed = [item for group in selected for item in group["removed"]]
    epoch_material = {
        "inventory_sha256": _sha256(inventory_raw),
        "removed_event_uuids": sorted(item["event_uuid"] for item in removed),
        "source_tip": source_tip,
        "survivor_event_uuids": sorted(group["survivor"]["event_uuid"] for group in selected),
    }
    epoch = f"comment-echo-reclaim-v1-{_sha256(_canonical(epoch_material))[:32]}"
    compat = _compat_transform(repo, compat_oid, epoch)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "incident": "2026-08 reconciler markerless comment echoes",
        "source_ref": args.source_ref,
        "source_tip": source_tip,
        "backup": {
            "bundle_sha256": _sha256_file(bundle, "backup bundle"),
            "manifest_sha256": _sha256(backup_raw),
            "bundle_refs": backup["bundle_refs"],
            "remote_refs": backup["remote_refs"],
        },
        "jira_inventory_sha256": _sha256(inventory_raw),
        "bridge": {
            "path": ".bridge_state/bindings.json",
            "blob_oid": bridge_oid,
            "blob_sha256": _sha256(bridge_raw),
            "jira_keys": jira_keys,
            "protected_comment_positions": sorted(comment_ids),
            "real_mapped_jira_ids": sorted(
                {value for value in comment_ids.values() if value != UNKNOWN_COMMENT_ID}
            ),
            "unknown_id_sentinel": UNKNOWN_COMMENT_ID,
        },
        "groups": selected,
        "snapshot_transforms": snapshots,
        "store_compat_transform": compat,
        "expected_delta": {
            "groups": len(selected),
            "removed_events": len(removed),
            "retained_echoes": len(selected),
            "event_bytes": sum(item["bytes"] for item in removed),
            "snapshot_bytes": sum(item["old_bytes"] - item["new_bytes"] for item in snapshots),
        },
    }
    payload["manifest_digest"] = _sha256(_canonical(payload))
    if _remote_snapshot(repo) != initial_remote:
        raise ReclaimError("remote heads/tags moved during manifest generation")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
        os.close(fd)
        temporary = Path(name)
        temporary.write_bytes(_canonical(payload) + b"\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return payload


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--source-tip", required=True)
    parser.add_argument("--backup-bundle", required=True, type=Path)
    parser.add_argument("--backup-manifest", required=True, type=Path)
    parser.add_argument("--jira-inventory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        manifest = build_manifest(parse_args(sys.argv[1:] if argv is None else argv))
    except ReclaimError as exc:
        sys.stderr.write(f"MANIFEST FAILED: {exc}\n")
        return 1
    delta = manifest["expected_delta"]
    sys.stdout.write(
        "MANIFEST READY: "
        f"{manifest['manifest_digest']}; {delta['removed_events']} events; "
        f"{delta['event_bytes'] + delta['snapshot_bytes']} logical bytes\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
