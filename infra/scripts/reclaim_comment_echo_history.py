#!/usr/bin/env python3
"""Build a verified, local-only history candidate from a comment-echo reclaim manifest."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO

import comment_echo_reclaim_manifest as manifest_tools
from prepare_reclaim_backup import _parse_remote_refs, _remote_snapshot
from reclaim_bridge_history import (
    ReclaimError,
    _copy_data_block,
    ref_exists,
    ref_name,
    rev_parse,
    run_git,
)

SCHEMA_VERSION = 1
STAGING_PREFIX = "refs/reclaim-comment-echo/"
SAFE_EXPORT_PATH = re.compile(r"[A-Za-z0-9._/-]+\Z")
HEX_DIGEST = re.compile(r"[0-9a-f]+\Z")
LIVE_FIELDS = ("id", "author_account_id", "normalized_body")
TOP_LEVEL = re.compile(rb"(?:blob|checkpoint|done)\n|(?:commit|feature|progress|reset|tag) ")


@dataclass(frozen=True)
class BlobRewrite:
    label: str
    old_oid: str
    new_oid: str
    aliases: tuple[str, ...]
    new_data: bytes


@dataclass(frozen=True)
class RewritePlan:
    manifest_raw: bytes
    manifest_digest: str
    source_ref: str
    source_name: str
    source_tip: str
    removed_paths: dict[str, tuple[str, str]]
    snapshot_paths: dict[str, BlobRewrite]
    compat: BlobRewrite
    compat_mode: str
    bridge_oid: str


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-ref", required=True)
    parser.add_argument("--commit-map", required=True, type=Path)
    return parser.parse_args(argv)


_canonical = manifest_tools._canonical
_sha256 = manifest_tools._sha256


def _require_hex(value: object, label: str, lengths: tuple[int, ...]) -> str:
    if not isinstance(value, str) or len(value) not in lengths or not HEX_DIGEST.fullmatch(value):
        raise ReclaimError(f"manifest {label} is not a canonical hexadecimal digest")
    return value


def _external_path(repo: Path, path: Path, label: str, *, must_exist: bool) -> Path:
    if path.absolute().is_symlink():
        raise ReclaimError(f"{label} must not be a symlink: {path.absolute()}")
    if must_exist:
        return manifest_tools._assert_regular_off_repo(repo, path, label)
    return manifest_tools._assert_new_off_repo(repo, path)


def _load_manifest(path: Path) -> tuple[dict[str, Any], bytes, str]:
    try:
        raw = manifest_tools._read_regular(path, "manifest")
        value = json.loads(raw)
    except ValueError as exc:
        raise ReclaimError(f"manifest is unreadable or invalid JSON: {path}") from exc
    if not isinstance(value, dict) or raw != _canonical(value) + b"\n":
        raise ReclaimError("manifest is not the exact canonical JSON artifact")
    digest = _require_hex(value.get("manifest_digest"), "digest", (64,))
    unsigned = dict(value)
    del unsigned["manifest_digest"]
    if not hmac.compare_digest(digest, _sha256(_canonical(unsigned))):
        raise ReclaimError("manifest digest does not bind its canonical content")
    return value, raw, digest


def _normalize_output_ref(repo: Path, requested: str, source_name: str) -> str:
    output = requested if requested.startswith("refs/") else f"refs/heads/{requested}"
    if not output.startswith("refs/heads/") or output.startswith(STAGING_PREFIX):
        raise ReclaimError("--output-ref must name a local refs/heads ref")
    run_git(repo, ["check-ref-format", output])
    if output == source_name:
        raise ReclaimError("--output-ref must differ from the source ref")
    if ref_exists(repo, output):
        raise ReclaimError(f"output ref already exists: {output}")
    return output


def _validate_backup(manifest: Mapping[str, Any], remote_snapshot: bytes, source_tip: str) -> None:
    backup = manifest.get("backup")
    if not isinstance(backup, dict):
        raise ReclaimError("manifest backup binding has an unsupported shape")
    _require_hex(backup.get("bundle_sha256"), "backup bundle digest", (64,))
    _require_hex(backup.get("manifest_sha256"), "backup manifest digest", (64,))
    bundle_refs = backup.get("bundle_refs")
    if not isinstance(bundle_refs, dict) or not bundle_refs:
        raise ReclaimError("manifest backup binding has no restore refs")
    valid_bundle = all(
        isinstance(name, str)
        and name.startswith("refs/reclaim-backup/")
        and isinstance(oid, str)
        and HEX_DIGEST.fullmatch(oid)
        and len(oid) in (40, 64)
        for name, oid in bundle_refs.items()
    )
    old_tips = [oid for name, oid in bundle_refs.items() if name.endswith("/old-tip")]
    if not valid_bundle or old_tips != [source_tip]:
        raise ReclaimError("manifest backup does not bind exactly one source-tip restore ref")
    recorded = backup.get("remote_refs")
    if not isinstance(recorded, list) or not recorded:
        raise ReclaimError("manifest backup has no live heads/tags census")
    expected: dict[str, tuple[str, str | None]] = {}
    for item in recorded:
        if not isinstance(item, dict):
            raise ReclaimError("manifest backup remote-ref census row is malformed")
        name, direct, peeled = (item.get(key) for key in ("ref", "direct_oid", "peeled_oid"))
        if not isinstance(name, str) or name in expected:
            raise ReclaimError("manifest backup remote-ref census is ambiguous")
        expected[name] = (
            _require_hex(direct, "backup remote OID", (40, 64)),
            None if peeled is None else _require_hex(peeled, "backup peeled OID", (40, 64)),
        )
    current = {
        ref.name: (ref.direct_oid, ref.peeled_oid) for ref in _parse_remote_refs(remote_snapshot)
    }
    if current != expected or expected.get("refs/heads/tickets") != (source_tip, None):
        raise ReclaimError("live remote heads/tags differ from the backup-bound census")


def _safe_export_path(path: object, label: str) -> str:
    if not isinstance(path, str) or not SAFE_EXPORT_PATH.fullmatch(path):
        raise ReclaimError(f"manifest {label} is not a safely framed export path")
    return path


def _validate_groups(
    repo: Path,
    paths: Mapping[str, str],
    bridge: Mapping[str, Any],
    groups: object,
) -> tuple[list[dict[str, Any]], dict[str, tuple[str, str]], set[str]]:
    if not isinstance(groups, list) or not groups:
        raise ReclaimError("manifest has no reclaim groups")
    declared: dict[str, list[str]] = {}
    inventory: dict[str, list[dict[str, Any]]] = {}
    cleanup_authority: dict[tuple[str, str], Mapping[str, str]] = {}
    identities: set[tuple[str, str]] = set()
    for group in groups:
        if not isinstance(group, dict):
            raise ReclaimError("manifest reclaim group is not an object")
        ticket = group.get("ticket_id")
        body_digest = _require_hex(group.get("body_sha256"), "group body digest", (64,))
        jira_key = group.get("jira_key")
        live = group.get("live_jira_comment")
        cleanup = group.get("jira_cleanup")
        cleanup_survivor = cleanup.get("survivor") if isinstance(cleanup, dict) else None
        cleanup_delete = cleanup.get("delete") if isinstance(cleanup, dict) else None
        authority = cleanup.get("authority") if isinstance(cleanup, dict) else None
        if (
            not isinstance(ticket, str)
            or not ticket
            or "/" in ticket
            or not isinstance(jira_key, str)
            or not isinstance(live, dict)
            or not all(isinstance(live.get(key), str) for key in LIVE_FIELDS)
            or not isinstance(live.get("body"), dict)
            or not isinstance(cleanup_survivor, dict)
            or not isinstance(cleanup_delete, dict)
            or not isinstance(authority, dict)
            or cleanup_survivor != live
            or not all(
                isinstance(comment.get(key), str)
                for comment in (cleanup_survivor, cleanup_delete)
                for key in LIVE_FIELDS
            )
            or not all(
                isinstance(comment.get("body"), dict)
                for comment in (cleanup_survivor, cleanup_delete)
            )
        ):
            raise ReclaimError("manifest reclaim group has an invalid ticket id")
        identity = (ticket, body_digest)
        if identity in identities:
            raise ReclaimError("manifest contains a duplicate reclaim group")
        identities.add(identity)
        declared.setdefault(ticket, []).append(body_digest)
        inventory.setdefault(jira_key, []).extend((cleanup_survivor, cleanup_delete))
        cleanup_authority[identity] = authority
    jira_keys, comment_ids = manifest_tools._bridge_constraints(bridge, declared)
    candidates = manifest_tools._candidate_events(repo, paths, declared)
    expected = manifest_tools._select_groups(
        candidates, declared, jira_keys, comment_ids, inventory, cleanup_authority
    )
    if groups != expected:
        raise ReclaimError("manifest reclaim groups differ from source/bridge evidence")
    removed_paths: dict[str, tuple[str, str]] = {}
    all_aliases: set[str] = set()
    for group in groups:
        actual = candidates[(group["ticket_id"], group["body_sha256"])]
        for item in actual:
            for alias in item["path_aliases"]:
                alias = _safe_export_path(alias, "COMMENT alias")
                if alias in all_aliases:
                    raise ReclaimError("manifest COMMENT aliases overlap")
                all_aliases.add(alias)
        for item in group["removed"]:
            for alias in item["path_aliases"]:
                removed_paths[alias] = (item["event_uuid"], item["blob_oid"])
    return groups, removed_paths, all_aliases


def _tree_entries(repo: Path, ref: str, paths: Sequence[str]) -> dict[str, tuple[str, str, str]]:
    if not paths:
        return {}
    raw = run_git(repo, ["ls-tree", "-z", ref, "--", *paths]).stdout
    entries: dict[str, tuple[str, str, str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_raw = record.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split(" ")
            path = path_raw.decode("utf-8", "surrogateescape")
        except ValueError as exc:
            raise ReclaimError("malformed git ls-tree output") from exc
        entries[path] = (mode, kind, oid)
    return entries


def _snapshot_rewrites(
    repo: Path,
    paths: Mapping[str, str],
    by_blob: Mapping[str, list[str]],
    groups: list[dict[str, Any]],
    snapshots: object,
    occupied_aliases: set[str],
) -> dict[str, BlobRewrite]:
    if not isinstance(snapshots, list):
        raise ReclaimError("manifest snapshot transforms must be a list")
    expected = manifest_tools._snapshot_transforms(repo, paths, groups)
    if snapshots != expected:
        raise ReclaimError("manifest snapshot transforms differ from source evidence")
    manifest_tools._assert_unshared(by_blob, groups, expected)
    removed_by_ticket: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        removed_by_ticket.setdefault(group["ticket_id"], []).extend(group["removed"])
    result: dict[str, BlobRewrite] = {}
    for item in expected:
        old = manifest_tools._read_blob(repo, item["old_blob_oid"])
        transformed = manifest_tools._transform_snapshot(old, removed_by_ticket[item["ticket_id"]])
        if transformed is None or transformed[0] == old:
            raise ReclaimError("manifest snapshot transform has no material source delta")
        rewrite = BlobRewrite(
            label=item["path"],
            old_oid=item["old_blob_oid"],
            new_oid=item["new_blob_oid"],
            aliases=tuple(item["path_aliases"]),
            new_data=transformed[0],
        )
        for alias in rewrite.aliases:
            alias = _safe_export_path(alias, "SNAPSHOT alias")
            if alias in occupied_aliases or alias in result:
                raise ReclaimError("manifest SNAPSHOT aliases overlap another target")
            result[alias] = rewrite
    return result


def _compat_rewrite(
    repo: Path, source_tip: str, paths: Mapping[str, str], manifest_value: object
) -> tuple[BlobRewrite, str]:
    if not isinstance(manifest_value, dict):
        raise ReclaimError("manifest store compatibility transform is malformed")
    path = _safe_export_path(manifest_value.get("path"), "store compatibility path")
    if path != ".store-compat.json":
        raise ReclaimError("manifest may transform only .store-compat.json compatibility state")
    old_oid = paths.get(path)
    epoch = manifest_value.get("epoch")
    if (
        old_oid is None
        or not isinstance(epoch, str)
        or not epoch.startswith("comment-echo-reclaim-v1-")
    ):
        raise ReclaimError("manifest has no source-bound reclaim epoch")
    expected = manifest_tools._compat_transform(repo, old_oid, epoch)
    if manifest_value != expected:
        raise ReclaimError("manifest store compatibility transform differs from source evidence")
    old = manifest_tools._read_blob(repo, old_oid)
    record = json.loads(old)
    record["epoch"] = epoch
    new = json.dumps(record, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    entry = _tree_entries(repo, source_tip, [path]).get(path)
    if entry is None or entry[1] != "blob" or entry[2] != old_oid:
        raise ReclaimError("source store compatibility entry is not a regular blob")
    rewrite = BlobRewrite(path, old_oid, expected["new_blob_oid"], (path,), new)
    return rewrite, entry[0]


def _validate_manifest(
    repo: Path,
    manifest: dict[str, Any],
    manifest_raw: bytes,
    digest: str,
    remote_snapshot: bytes,
) -> RewritePlan:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ReclaimError("manifest has an unsupported schema")
    source_ref = manifest.get("source_ref")
    source_tip = _require_hex(manifest.get("source_tip"), "source tip", (40, 64))
    if not isinstance(source_ref, str) or not source_ref:
        raise ReclaimError("manifest source ref is invalid")
    manifest_tools._assert_source(repo, source_ref, source_tip)
    source_name = ref_name(repo, source_ref)
    if not source_name.startswith("refs/heads/"):
        raise ReclaimError("manifest source must name a local branch")
    _validate_backup(manifest, remote_snapshot, source_tip)
    paths, by_blob = manifest_tools._tree(repo, source_tip)
    bridge_manifest = manifest.get("bridge")
    if not isinstance(bridge_manifest, dict):
        raise ReclaimError("manifest bridge binding is malformed")
    bridge_oid = paths.get(".bridge_state/bindings.json")
    if bridge_oid is None:
        raise ReclaimError("source tip lacks .bridge_state/bindings.json")
    bridge_raw = manifest_tools._read_blob(repo, bridge_oid)
    try:
        bridge = json.loads(bridge_raw)
    except ValueError as exc:
        raise ReclaimError("source bridge bindings are invalid JSON") from exc
    if not isinstance(bridge, dict):
        raise ReclaimError("source bridge bindings have an unsupported shape")
    groups, removed_paths, occupied = _validate_groups(repo, paths, bridge, manifest.get("groups"))
    declared = {
        ticket: [group["body_sha256"] for group in groups if group["ticket_id"] == ticket]
        for ticket in {group["ticket_id"] for group in groups}
    }
    jira_keys, comment_ids = manifest_tools._bridge_constraints(bridge, declared)
    expected_bridge = {
        "path": ".bridge_state/bindings.json",
        "blob_oid": bridge_oid,
        "blob_sha256": _sha256(bridge_raw),
        "jira_keys": jira_keys,
        "protected_comment_positions": sorted(comment_ids),
        "real_mapped_jira_ids": sorted(
            {value for value in comment_ids.values() if value != manifest_tools.UNKNOWN_COMMENT_ID}
        ),
        "unknown_id_sentinel": manifest_tools.UNKNOWN_COMMENT_ID,
    }
    if bridge_manifest != expected_bridge:
        raise ReclaimError("manifest bridge binding differs from source bytes")
    snapshots = _snapshot_rewrites(
        repo,
        paths,
        by_blob,
        groups,
        manifest.get("snapshot_transforms"),
        occupied,
    )
    compat, compat_mode = _compat_rewrite(
        repo, source_tip, paths, manifest.get("store_compat_transform")
    )
    if compat.aliases[0] in occupied or compat.aliases[0] in snapshots:
        raise ReclaimError("manifest compatibility path overlaps another target")
    removed = [item for group in groups for item in group["removed"]]
    snapshot_values = manifest["snapshot_transforms"]
    expected_delta = {
        "groups": len(groups),
        "removed_events": len(removed),
        "retained_echoes": len(groups),
        "event_bytes": sum(item["bytes"] for item in removed),
        "snapshot_bytes": sum(item["old_bytes"] - item["new_bytes"] for item in snapshot_values),
    }
    if manifest.get("expected_delta") != expected_delta:
        raise ReclaimError("manifest expected delta does not match its enumerated mutations")
    return RewritePlan(
        manifest_raw,
        digest,
        source_ref,
        source_name,
        source_tip,
        removed_paths,
        snapshots,
        compat,
        compat_mode,
        bridge_oid,
    )


def _materialize_new_blobs(repo: Path, plan: RewritePlan) -> None:
    rewrites = {rewrite.new_oid: rewrite for rewrite in plan.snapshot_paths.values()}
    rewrites[plan.compat.new_oid] = plan.compat
    for expected_oid, rewrite in rewrites.items():
        actual_oid = (
            run_git(repo, ["hash-object", "-w", "--stdin"], input_bytes=rewrite.new_data)
            .stdout.decode()
            .strip()
        )
        if actual_oid != expected_oid:
            raise ReclaimError(f"derived blob differs from manifest for {rewrite.label}")


def _filter_export(
    source: BinaryIO,
    destination: BinaryIO,
    plan: RewritePlan,
    staging_ref: str,
    expected_commits: set[str],
) -> dict[str, str]:
    commit_marks: dict[str, str] = {}
    current = False
    current_oid: str | None = None
    current_mark: str | None = None
    compat_injected = False
    staging_raw = staging_ref.encode("ascii")

    def finish_commit() -> None:
        nonlocal current, current_oid, current_mark, compat_injected
        if not current:
            return
        if current_oid is None or current_mark is None or current_oid in commit_marks:
            raise ReclaimError("fast-export commit lacks a unique original OID and mark")
        if current_oid == plan.source_tip:
            if compat_injected:
                raise ReclaimError("source tip appeared more than once in fast-export")
            destination.write(
                b"M "
                + plan.compat_mode.encode("ascii")
                + b" "
                + plan.compat.new_oid.encode("ascii")
                + b" "
                + plan.compat.aliases[0].encode("ascii")
                + b"\n"
            )
            compat_injected = True
        commit_marks[current_oid] = current_mark
        current = False
        current_oid = None
        current_mark = None

    while line := source.readline():
        if line.startswith(b"data "):
            _copy_data_block(source, destination, line)
            continue
        if current and line == b"\n":
            finish_commit()
            destination.write(line)
            continue
        if TOP_LEVEL.match(line):
            finish_commit()
            if line.startswith((b"commit ", b"reset ")):
                target_ref = line.rstrip(b"\n").split(b" ", 1)[1]
                if target_ref != staging_raw:
                    raise ReclaimError("fast-export attempted to address an unexpected ref")
            if line.startswith(b"tag ") or line.startswith(b"blob\n"):
                raise ReclaimError("fast-export emitted an unexpected tag or blob record")
            current = line.startswith(b"commit ")
        elif current and line.startswith(b"original-oid "):
            if current_oid is not None:
                raise ReclaimError("fast-export commit has duplicate original OIDs")
            current_oid = line.rstrip(b"\n").split(b" ", 1)[1].decode("ascii")
        elif current and line.startswith(b"mark "):
            if current_mark is not None:
                raise ReclaimError("fast-export commit has duplicate marks")
            current_mark = line.rstrip(b"\n").split(b" ", 1)[1].decode("ascii")
        elif line.startswith(b"M "):
            fields = line.rstrip(b"\n").split(b" ", 3)
            if len(fields) != 4:
                raise ReclaimError("malformed fast-export file modification")
            _command, mode, dataref, raw_path = fields
            try:
                path = raw_path.decode("ascii", "strict")
            except UnicodeDecodeError:
                destination.write(line)
                continue
            removed = plan.removed_paths.get(path)
            snapshot = plan.snapshot_paths.get(path)
            if removed is not None:
                _label, old_oid = removed
                if dataref != old_oid.encode("ascii"):
                    raise ReclaimError(f"COMMENT path {path} referenced an unexpected old blob")
                continue
            if snapshot is not None:
                if dataref != snapshot.old_oid.encode("ascii"):
                    raise ReclaimError(f"SNAPSHOT path {path} referenced an unexpected old blob")
                destination.write(
                    b"M " + mode + b" " + snapshot.new_oid.encode("ascii") + b" " + raw_path + b"\n"
                )
                continue
        elif line.startswith(b"D "):
            raw_path = line[2:].rstrip(b"\n")
            try:
                path = raw_path.decode("ascii", "strict")
            except UnicodeDecodeError:
                path = ""
            if path in plan.removed_paths:
                continue
        elif line.startswith((b"R ", b"C ")) or line.rstrip(b"\n") == b"deleteall":
            raise ReclaimError("fast-export emitted an unsupported tree-wide/rename command")
        destination.write(line)
    finish_commit()
    if set(commit_marks) != expected_commits:
        raise ReclaimError("fast-export did not enumerate the complete source commit graph")
    if not compat_injected:
        raise ReclaimError("fast-export did not expose the exact source tip")
    return commit_marks


def _read_marks(path: Path) -> dict[str, str]:
    marks: dict[str, str] = {}
    try:
        lines = path.read_bytes().splitlines()
    except OSError as exc:
        raise ReclaimError("git fast-import did not emit its commit marks") from exc
    for line in lines:
        fields = line.split()
        if len(fields) != 2:
            raise ReclaimError("git fast-import emitted a malformed marks file")
        mark, oid = (field.decode("ascii") for field in fields)
        if mark in marks:
            raise ReclaimError("git fast-import emitted a duplicate mark")
        marks[mark] = _require_hex(oid, "imported commit OID", (40, 64))
    return marks


def _export_import(
    repo: Path,
    plan: RewritePlan,
    staging_ref: str,
    expected_commits: set[str],
) -> dict[str, str]:
    git_env = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}
    export_command = [
        "git",
        "-C",
        str(repo),
        "fast-export",
        "--no-data",
        "--show-original-ids",
        "--signed-tags=strip",
        "--signed-commits=strip",
        "--reencode=no",
        "--use-done-feature",
        f"--refspec={plan.source_name}:{staging_ref}",
        "--",
        plan.source_name,
    ]
    with tempfile.TemporaryDirectory(prefix="reclaim-comment-echo-") as temporary:
        marks_path = Path(temporary) / "import.marks"
        with tempfile.TemporaryFile() as exported, tempfile.TemporaryFile() as filtered:
            result = subprocess.run(
                export_command,
                stdout=exported,
                stderr=subprocess.PIPE,
                env=git_env,
                check=False,
            )
            if result.returncode:
                detail = result.stderr.decode(errors="replace").strip()
                raise ReclaimError(f"git fast-export failed: {detail}")
            exported.seek(0)
            commit_marks = _filter_export(exported, filtered, plan, staging_ref, expected_commits)
            filtered.seek(0)
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "fast-import",
                    "--quiet",
                    f"--export-marks={marks_path}",
                ],
                stdin=filtered,
                capture_output=True,
                env=git_env,
                check=False,
            )
        if result.returncode:
            detail = (result.stderr or result.stdout).decode(errors="replace").strip()
            raise ReclaimError(f"git fast-import failed: {detail}")
        marks = _read_marks(marks_path)
    missing = set(commit_marks.values()) - set(marks)
    if missing:
        raise ReclaimError("git fast-import omitted a source commit mark")
    return {old: marks[mark] for old, mark in commit_marks.items()}


def _graph(repo: Path, ref: str) -> dict[str, list[str]]:
    graph: dict[str, list[str]] = {}
    for line in run_git(repo, ["rev-list", "--parents", ref]).stdout.decode().splitlines():
        oid, *parents = line.split()
        if oid in graph:
            raise ReclaimError("git rev-list returned a duplicate commit")
        graph[oid] = parents
    if not graph:
        raise ReclaimError("source ref has no commit graph")
    return graph


def _commit_identities(repo: Path, commits: Sequence[str]) -> dict[str, tuple[bytes, bytes, bytes]]:
    ordered = tuple(dict.fromkeys(commits))
    queries = b"".join(f"{oid}\n".encode() for oid in ordered)
    stream = BytesIO(run_git(repo, ["cat-file", "--batch"], input_bytes=queries).stdout)
    identities: dict[str, tuple[bytes, bytes, bytes]] = {}
    for commit in ordered:
        if (fields := stream.readline().split())[:2] != [commit.encode(), b"commit"]:
            raise ReclaimError(f"commit {commit} has malformed batch metadata")
        raw = stream.read(int(fields[2]))
        stream.read(1)
        headers, separator, message = raw.partition(b"\n\n")
        authors = [line for line in headers.splitlines() if line.startswith(b"author ")]
        committers = [line for line in headers.splitlines() if line.startswith(b"committer ")]
        if not separator or len(authors) != 1 or len(committers) != 1:
            raise ReclaimError(f"commit {commit} has incomplete identity metadata")
        identities[commit] = (authors[0], committers[0], message)
    return identities


def _verify_rewrite(
    repo: Path,
    plan: RewritePlan,
    staging_ref: str,
    source_graph: Mapping[str, list[str]],
    commit_map: Mapping[str, str],
) -> str:
    if set(commit_map) != set(source_graph) or len(set(commit_map.values())) != len(commit_map):
        raise ReclaimError("commit map is not a complete one-to-one source mapping")
    output_tip = rev_parse(repo, staging_ref)
    if commit_map.get(plan.source_tip) != output_tip:
        raise ReclaimError("commit map does not bind the source and output tips")
    output_graph = _graph(repo, output_tip)
    if set(output_graph) != set(commit_map.values()):
        raise ReclaimError("rewritten ancestry differs from the mapped commit set")
    identities = _commit_identities(repo, [*source_graph, *output_graph])

    for old, parents in source_graph.items():
        new = commit_map[old]
        if output_graph[new] != [commit_map[parent] for parent in parents]:
            raise ReclaimError(f"rewritten parent topology differs at {old}")
        if identities[old] != identities[new]:
            raise ReclaimError(f"rewritten author/committer/message differs at {old}")
    target_paths = tuple(
        sorted({*plan.removed_paths, *plan.snapshot_paths, plan.compat.aliases[0]})
    )
    head_entries = _tree_entries(repo, output_tip, target_paths)
    if any(path in head_entries for path in plan.removed_paths):
        raise ReclaimError("rewritten head retains a removed COMMENT alias")
    for rewrite in {item.label: item for item in plan.snapshot_paths.values()}.values():
        if head_entries.get(rewrite.label, (None, None, None))[2] != rewrite.new_oid:
            raise ReclaimError(f"rewritten head lacks SNAPSHOT substitution: {rewrite.label}")
    if head_entries.get(plan.compat.aliases[0], (None, None, None))[2] != plan.compat.new_oid:
        raise ReclaimError("rewritten head lacks the manifest compatibility epoch")
    run_git(repo, ["fsck", "--strict", output_tip])
    bridge = (
        run_git(repo, ["rev-parse", f"{output_tip}:.bridge_state/bindings.json"])
        .stdout.decode()
        .strip()
    )
    if bridge != plan.bridge_oid:
        raise ReclaimError("rewritten head changed .bridge_state bytes")
    return output_tip


def _local_refs(repo: Path) -> dict[str, str]:
    raw = run_git(repo, ["for-each-ref", "--format=%(refname)%00%(objectname)"]).stdout
    refs: dict[str, str] = {}
    for line in raw.splitlines():
        try:
            name_raw, oid_raw = line.split(b"\0", 1)
        except ValueError as exc:
            raise ReclaimError("malformed local ref census") from exc
        refs[name_raw.decode()] = oid_raw.decode("ascii")
    return refs


def _assert_quiescent(
    repo: Path,
    plan: RewritePlan,
    manifest_path: Path,
    initial_remote: bytes,
) -> None:
    if _remote_snapshot(repo) != initial_remote:
        raise ReclaimError("live remote heads/tags moved during rewrite")
    if (
        manifest_path.read_bytes() != plan.manifest_raw
        or rev_parse(repo, plan.source_ref) != plan.source_tip
        or run_git(repo, ["status", "--porcelain"]).stdout
    ):
        raise ReclaimError("manifest, source, or live remote changed during rewrite")


def _write_commit_map(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(fd, "wb") as stream:
            stream.write(_canonical(payload) + b"\n")
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
        raise


def _delete_ref(repo: Path | None, ref: str | None) -> None:
    if repo is None or ref is None:
        return
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "-d", ref],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def rewrite(args: argparse.Namespace) -> dict[str, object]:
    repo: Path | None = None
    output_ref: str | None = None
    staging_ref: str | None = None
    commit_map_path: Path | None = None
    output_created = False
    map_created = False
    complete = False
    try:
        repo = args.repo.resolve()
        git_dir = repo / ".git"
        if not repo.is_dir() or not git_dir.is_dir() or git_dir.is_symlink():
            raise ReclaimError(f"not a standalone Git worktree: {repo}")
        manifest_path = _external_path(repo, args.manifest, "manifest", must_exist=True)
        commit_map_path = _external_path(repo, args.commit_map, "commit map", must_exist=False)
        if manifest_path == commit_map_path:
            raise ReclaimError("manifest and commit map paths must differ")
        manifest, manifest_raw, digest = _load_manifest(manifest_path)
        initial_remote = _remote_snapshot(repo)
        plan = _validate_manifest(repo, manifest, manifest_raw, digest, initial_remote)
        output_ref = _normalize_output_ref(repo, args.output_ref, plan.source_name)
        initial_refs = _local_refs(repo)
        _materialize_new_blobs(repo, plan)
        source_graph = _graph(repo, plan.source_tip)
        staging_ref = f"{STAGING_PREFIX}{secrets.token_hex(16)}"
        while ref_exists(repo, staging_ref):
            staging_ref = f"{STAGING_PREFIX}{secrets.token_hex(16)}"
        commits = _export_import(repo, plan, staging_ref, set(source_graph))
        output_tip = _verify_rewrite(repo, plan, staging_ref, source_graph, commits)
        refs_during = _local_refs(repo)
        if refs_during.pop(staging_ref, None) != output_tip or refs_during != initial_refs:
            raise ReclaimError("rewrite changed a local ref outside its staging namespace")
        _assert_quiescent(repo, plan, manifest_path, initial_remote)
        null_oid = "0" * len(output_tip)
        run_git(repo, ["update-ref", output_ref, output_tip, null_oid])
        output_created = True
        run_git(repo, ["update-ref", "-d", staging_ref, output_tip])
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "manifest_digest": plan.manifest_digest,
            "source_tip": plan.source_tip,
            "output_tip": output_tip,
            "commits": commits,
        }
        _write_commit_map(commit_map_path, payload)
        map_created = True
        _assert_quiescent(repo, plan, manifest_path, initial_remote)
        expected_refs = {**initial_refs, output_ref: output_tip}
        if _local_refs(repo) != expected_refs:
            raise ReclaimError("published candidate changed an unexpected local ref")
        complete = True
        return payload
    except ReclaimError:
        raise
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        raise ReclaimError(f"rewrite could not be completed safely: {exc}") from exc
    finally:
        _delete_ref(repo, staging_ref)
        if not complete:
            if output_created:
                _delete_ref(repo, output_ref)
            if map_created and commit_map_path is not None:
                commit_map_path.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        rewrite(parse_args(sys.argv[1:] if argv is None else argv))
    except ReclaimError as exc:
        sys.stderr.write(f"REWRITE FAILED: {exc}\n")
        return 1
    sys.stdout.write("REWRITE READY: local candidate verified; no push was performed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
