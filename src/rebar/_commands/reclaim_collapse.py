"""Offline shadow-clone history collapse for ADR 0106 reclamation.

S1 stops at building and validating a rewritten shadow branch. It deliberately has no
publish/swap path: callers must pass an explicitly marked disposable tracker clone and
the engine refuses any target with configured remotes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from rebar._mcp_errors import js_safe_dumps
from rebar._store.gitutil import run_git
from rebar.reducer import reduce_all_tickets

_SHADOW_MARKER = Path(".rebar") / "reclaim-shadow-clone"
_SCRATCH_REF = "refs/rebar/reclaim-collapse/result"
_DRY_RUN_REF = "refs/rebar/reclaim-collapse/dry-run"


class ReclaimCollapseError(RuntimeError):
    """Base error for the offline reclaim-collapse engine."""


class ShadowSafetyError(ReclaimCollapseError):
    """The target is not an explicitly disposable, offline shadow clone."""


@dataclass(frozen=True)
class CollapseResult:
    """Observable result of one offline collapse attempt."""

    applied: bool
    original_tip: str
    boundary_commit: str
    checkpoint_sha: str
    ledger_anchor_sha: str
    rewritten_tip: str
    enforce_since: str
    collapsed_commits: int
    replayed_commits: int
    ledger_entries_rewritten: int
    used_fast_import: bool = True

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def reduce_shadow_tracker(shadow_tracker: str | os.PathLike[str]) -> object:
    """Return reduced state for the explicitly supplied shadow tracker."""

    return _normalize_reduced_state(reduce_all_tickets(str(Path(shadow_tracker))))


# raw-git-ok: mutates only the safety-checked disposable shadow clone
def collapse_shadow_tracker(
    shadow_tracker: str | os.PathLike[str],
    *,
    boundary_commit: str,
    apply: bool = False,
    branch: str = "HEAD",
    now: datetime | None = None,
) -> CollapseResult:
    """Collapse history at ``boundary_commit`` inside a disposable shadow tracker.

    The target ref is only updated when ``apply`` is true. Dry-runs still exercise the
    core-git fast-import path against a temporary ref, then delete that ref; HEAD is left
    unchanged.
    """

    tracker = Path(shadow_tracker).resolve()
    _assert_shadow_tracker(tracker)
    boundary = _rev_parse(tracker, boundary_commit)
    original_tip = _rev_parse(tracker, branch)
    if not _is_ancestor(tracker, boundary, original_tip):
        raise ReclaimCollapseError(f"boundary {boundary} is not an ancestor of {original_tip}")

    # Enforce ADR-0106 eligibility when the shadow clone includes the copied remote-tracking
    # horizon ref. Tiny offline fixtures may omit that ref; prod publishing remains S2.
    if _has_remote_horizon_ref(tracker) and not _remote_horizon_eligible(
        tracker, boundary, now=now
    ):
        raise ReclaimCollapseError(
            f"boundary {boundary} is not eligible under the remote-anchored reclaim horizon"
        )

    original_position_map = _position_commit_map_for_tracker(tracker)
    ref = _SCRATCH_REF if apply else _DRY_RUN_REF
    _delete_ref(tracker, ref)

    checkpoint_sha, _ = _fast_import_tree_commit(
        tracker,
        ref,
        boundary,
        message=_commit_message(tracker, boundary),
        metadata=_commit_metadata(tracker, boundary),
        parents=(),
        old_to_new={},
        original_position_map=original_position_map,
        boundary=boundary,
    )
    old_to_new: dict[str, str] = {boundary: checkpoint_sha}

    ledger_anchor_sha, rewritten_at_anchor = _fast_import_tree_commit(
        tracker,
        ref,
        boundary,
        message=b"rebar reclaim: reconcile authorship ledger\n",
        metadata=_derived_metadata(),
        parents=(checkpoint_sha,),
        old_to_new=old_to_new,
        original_position_map=original_position_map,
        boundary=boundary,
    )

    commits = _rev_list(tracker, f"{boundary}..{original_tip}")
    current_tip = ledger_anchor_sha
    ledger_rewrites = rewritten_at_anchor
    for commit in commits:
        parents = _rewritten_parents(tracker, commit, boundary, current_tip, old_to_new)
        current_tip, rewrites = _fast_import_tree_commit(
            tracker,
            ref,
            commit,
            message=_commit_message(tracker, commit),
            metadata=_commit_metadata(tracker, commit),
            parents=parents,
            old_to_new=old_to_new | {boundary: checkpoint_sha},
            original_position_map=original_position_map,
            boundary=boundary,
        )
        old_to_new[commit] = current_tip
        ledger_rewrites += rewrites

    original_count = int(_git(tracker, "rev-list", "--count", original_tip).stdout)
    collapsed = max(0, original_count - len(commits))
    if apply:
        _update_current_branch(tracker, current_tip, original_tip)
        # raw-git-ok: resets only the safety-checked shadow clone worktree
        _git(
            tracker,
            "reset",
            "--hard",
            "-q",
            current_tip,
        )
        _delete_ref(tracker, ref)
    else:
        _delete_ref(tracker, ref)

    return CollapseResult(
        applied=apply,
        original_tip=original_tip,
        boundary_commit=boundary,
        checkpoint_sha=checkpoint_sha,
        ledger_anchor_sha=ledger_anchor_sha,
        rewritten_tip=current_tip,
        enforce_since=ledger_anchor_sha,
        collapsed_commits=collapsed,
        replayed_commits=len(commits),
        ledger_entries_rewritten=ledger_rewrites,
    )


def cli(argv: list[str]) -> int:
    from rebar._cli._parsers.core.reclaim import build_reclaim_collapse

    parser = build_reclaim_collapse(prog="rebar reclaim-collapse")
    args = parser.parse_args(argv)
    try:
        result = collapse_shadow_tracker(
            args.shadow_tracker,
            boundary_commit=args.boundary,
            apply=args.apply,
            branch=args.branch,
        )
    except (ReclaimCollapseError, ShadowSafetyError, subprocess.CalledProcessError) as exc:
        print(f"reclaim-collapse: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(js_safe_dumps(result.to_dict()))
    else:
        action = "applied" if result.applied else "dry-run"
        print(
            f"reclaim-collapse {action}: {result.original_tip} -> {result.rewritten_tip}; "
            f"checkpoint {result.checkpoint_sha}; enforce_since {result.enforce_since}"
        )
    return 0


def _assert_shadow_tracker(tracker: Path) -> None:
    if not tracker.exists():
        raise ShadowSafetyError(f"shadow tracker does not exist: {tracker}")
    if not (tracker / _SHADOW_MARKER).is_file():
        raise ShadowSafetyError(
            f"refusing to rewrite {tracker}: missing disposable shadow marker {_SHADOW_MARKER}"
        )
    if _git(tracker, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        raise ShadowSafetyError(f"{tracker} is not a git worktree")
    remotes = _git(tracker, "remote", check=False).stdout.splitlines()
    if remotes:
        raise ShadowSafetyError(
            f"refusing to rewrite {tracker}: shadow target has push-capable remote(s) "
            f"{', '.join(sorted(remotes))}"
        )
    try:
        from rebar import config

        live = config.tracker_dir(None).resolve()
        if live == tracker:
            raise ShadowSafetyError(f"refusing to rewrite live configured tracker {tracker}")
    except ShadowSafetyError:
        raise
    except (config.ConfigError, OSError, RuntimeError):
        pass


def _remote_horizon_eligible(tracker: Path, commit: str, *, now: datetime | None) -> bool:
    from rebar._store.reclaim_eligibility import checkpoint_commit_eligible

    try:
        return checkpoint_commit_eligible(str(tracker), commit, now=now)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _has_remote_horizon_ref(tracker: Path) -> bool:
    try:
        from rebar import config

        remote = config.tickets_remote(tracker)
        branch = config.tickets_branch(tracker)
    except (config.ConfigError, OSError, RuntimeError):
        return False
    ref = f"refs/remotes/{remote}/{branch}"
    return _git(tracker, "show-ref", "--verify", "--quiet", ref, check=False).returncode == 0


def _normalize_reduced_state(state: object) -> object:
    def scrub(value):
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in sorted(value.items()) if k not in {"updated_at"}}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value

    return scrub(state)


# raw-git-ok: offline shadow-clone rewrite engine; target is safety-checked and never live store
def _git(
    tracker: Path,
    *args: str,
    check: bool = True,
    input_data: bytes | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    return run_git(
        tracker,
        *args,
        check=check,
        text=text,
        input_data=input_data,
    )


def _rev_parse(tracker: Path, rev: str) -> str:
    return _git(tracker, "rev-parse", "--verify", f"{rev}^{{commit}}").stdout.strip()


def _is_ancestor(tracker: Path, ancestor: str, descendant: str) -> bool:
    result = _git(tracker, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
    return result.returncode == 0


def _rev_list(tracker: Path, rev_range: str) -> list[str]:
    out = _git(tracker, "rev-list", "--reverse", "--topo-order", rev_range).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


# raw-git-ok: mutates only scratch refs in the safety-checked disposable shadow clone
def _delete_ref(tracker: Path, ref: str) -> None:
    # raw-git-ok: deletes only scratch refs in the safety-checked shadow clone
    _git(
        tracker,
        "update-ref",
        "-d",
        ref,
        check=False,
    )


# raw-git-ok: advances only the safety-checked disposable shadow clone branch
def _update_current_branch(tracker: Path, new: str, old: str) -> None:
    branch = _git(tracker, "symbolic-ref", "-q", "HEAD").stdout.strip()
    if not branch:
        raise ShadowSafetyError("shadow tracker must be on a branch, not detached HEAD")
    # raw-git-ok: advances only the safety-checked shadow clone branch
    _git(
        tracker,
        "update-ref",
        branch,
        new,
        old,
    )


def _commit_metadata(tracker: Path, commit: str) -> tuple[str, str, str, str, str, str]:
    fmt = "%an%x00%ae%x00%at +0000%x00%cn%x00%ce%x00%ct +0000"
    parts = _git(tracker, "show", "-s", f"--format={fmt}", commit).stdout.split("\x00")
    if len(parts) != 6:
        raise ReclaimCollapseError(f"cannot read commit metadata for {commit}")
    return (
        parts[0].strip(),
        parts[1].strip(),
        parts[2].strip(),
        parts[3].strip(),
        parts[4].strip(),
        parts[5].strip(),
    )


def _commit_message(tracker: Path, commit: str) -> bytes:
    return _git(tracker, "log", "-1", "--format=%B", commit, input_data=None).stdout.encode()


def _tree_entries(tracker: Path, treeish: str) -> list[tuple[str, str, str, bytes]]:
    out = _git(tracker, "ls-tree", "-rz", treeish, text=False).stdout
    entries: list[tuple[str, str, str, bytes]] = []
    for raw in out.split(b"\x00"):
        if not raw:
            continue
        meta, path = raw.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        entries.append((mode, kind, oid, path))
    return entries


def _fast_import_tree_commit(
    tracker: Path,
    ref: str,
    treeish: str,
    *,
    message: bytes,
    metadata: tuple[str, str, str, str, str, str],
    parents: tuple[str, ...],
    old_to_new: Mapping[str, str],
    original_position_map: Mapping[str, str],
    boundary: str,
) -> tuple[str, int]:
    _delete_ref(tracker, ref)
    an, ae, ad, cn, ce, cd = metadata
    chunks: list[bytes] = [
        f"commit {ref}\n".encode(),
        f"author {an} <{ae}> {ad}\n".encode(),
        f"committer {cn} <{ce}> {cd}\n".encode(),
        f"data {len(message)}\n".encode(),
        message,
    ]
    if parents:
        chunks.append(f"from {parents[0]}\n".encode())
        for parent in parents[1:]:
            chunks.append(f"merge {parent}\n".encode())
    chunks.append(b"deleteall\n")
    rewrite_count = 0
    for mode, kind, oid, path in _tree_entries(tracker, treeish):
        if kind != "blob":
            continue
        blob = _blob_bytes_for_entry(tracker, treeish, path)
        rewritten, changed_entries = _rewrite_snapshot_blob(
            blob, original_position_map, old_to_new, boundary, tracker
        )
        if rewritten is None:
            chunks.append(
                b"M " + mode.encode() + b" " + oid.encode() + b" " + _quote_path(path) + b"\n"
            )
        else:
            rewrite_count += changed_entries
            chunks.append(b"M " + mode.encode() + b" inline " + _quote_path(path) + b"\n")
            chunks.append(f"data {len(rewritten)}\n".encode() + rewritten)
    chunks.append(b"\n")
    imported = _git(
        tracker,
        "fast-import",
        "--quiet",
        "--date-format=raw",
        input_data=b"".join(chunks),
        text=False,
        check=False,
    )
    if imported.returncode != 0:
        stderr = (
            imported.stderr.decode("utf-8", "replace")
            if isinstance(imported.stderr, bytes)
            else imported.stderr
        )
        raise ReclaimCollapseError(f"git fast-import failed: {stderr}")
    return _rev_parse(tracker, ref), rewrite_count


def _blob_bytes_for_entry(tracker: Path, treeish: str, path: bytes) -> bytes:
    return _git(tracker, "show", f"{treeish}:{path.decode()}", text=False).stdout


def _quote_path(path: bytes) -> bytes:
    text = path.decode("utf-8")
    if all(ch not in text for ch in ' \t\n"\\'):
        return path
    return json.dumps(text, ensure_ascii=False).encode("utf-8")


def _rewrite_snapshot_blob(
    blob: bytes,
    original_position_map: Mapping[str, str],
    old_to_new: Mapping[str, str],
    boundary: str,
    tracker: Path,
) -> tuple[bytes | None, int]:
    try:
        event = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, 0
    if event.get("event_type") != "SNAPSHOT":
        return None, 0
    count = _rewrite_ledger_in_event(event, original_position_map, old_to_new, boundary, tracker)
    if count == 0:
        return None, 0
    return json.dumps(event, sort_keys=True, separators=(",", ":")).encode() + b"\n", count


def _rewrite_ledger_in_event(
    event: dict,
    original_position_map: Mapping[str, str],
    old_to_new: Mapping[str, str],
    boundary: str,
    tracker: Path,
) -> int:
    ledger = event.get("data", {}).get("compiled_state", {}).get("authorship_ledger")
    if not isinstance(ledger, list):
        return 0
    changed = 0
    for entry in ledger:
        pos = entry.get("position") if isinstance(entry, dict) else None
        if not isinstance(pos, dict):
            continue
        position = pos.get("position")
        if not isinstance(position, str):
            continue
        old_commit = original_position_map.get(position) or pos.get("commit_sha")
        new_commit = _map_old_commit(tracker, old_commit, boundary, old_to_new)
        if new_commit and pos.get("commit_sha") != new_commit:
            pos["commit_sha"] = new_commit
            changed += 1
    return changed


def _map_old_commit(
    tracker: Path, old_commit: object, boundary: str, old_to_new: Mapping[str, str]
) -> str | None:
    if not isinstance(old_commit, str) or not old_commit:
        return None
    if old_commit in old_to_new:
        return old_to_new[old_commit]
    if _is_ancestor(tracker, old_commit, boundary):
        return old_to_new.get(boundary)
    return None


def _derived_metadata() -> tuple[str, str, str, str, str, str]:
    now = f"{int(datetime.now(UTC).timestamp())} +0000"
    return (
        "rebar",
        "rebar@example.invalid",
        now,
        "rebar",
        "rebar@example.invalid",
        now,
    )


def _rewritten_parents(
    tracker: Path,
    commit: str,
    boundary: str,
    current_tip: str,
    old_to_new: Mapping[str, str],
) -> tuple[str, ...]:
    parents = _git(tracker, "show", "-s", "--format=%P", commit).stdout.split()
    rewritten: list[str] = []
    for parent in parents:
        mapped = old_to_new.get(parent)
        if mapped is None and _is_ancestor(tracker, parent, boundary):
            mapped = current_tip
        if mapped is not None and mapped not in rewritten:
            rewritten.append(mapped)
    return tuple(rewritten or [current_tip])


def _position_commit_map_for_tracker(tracker: Path) -> dict[str, str]:
    # Use the same full-history algorithm as rebar.attest.authorship_resolution, scoped to an
    # explicit shadow tracker rather than the configured live tracker.
    out = _git(
        tracker,
        "log",
        "--diff-filter=A",
        "--full-history",
        "--no-merges",
        "--no-renames",
        "--format=%x1e%H",
        "--name-only",
        "--",
        "*.json",
        check=False,
    ).stdout
    position_map: dict[str, str] = {}
    for record in out.split("\x1e"):
        if not record.strip():
            continue
        lines = record.split("\n")
        sha = lines[0].strip()
        if len(sha) != 40:
            continue
        for path in lines[1:]:
            path = path.strip()
            if not path.endswith(".json"):
                continue
            base = os.path.basename(path)[: -len(".json")]
            position = base.rsplit("-", 1)[0] if "-" in base else base
            if position:
                position_map[position] = sha
    return position_map
