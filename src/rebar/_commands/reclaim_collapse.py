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
from collections.abc import Set as AbstractSet
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rebar._commands.reclaim_collapse_stream import FastImportSession, ObjectReader, StreamError
from rebar._mcp_errors import js_safe_dumps
from rebar._store.gitutil import run_git
from rebar.reducer import reduce_all_tickets

if TYPE_CHECKING:
    from rebar._commands.reclaim_resign import ResignPlan

_SHADOW_MARKER = Path(".rebar") / "reclaim-shadow-clone"
_SCRATCH_REF = "refs/rebar/reclaim-collapse/result"
_DRY_RUN_REF = "refs/rebar/reclaim-collapse/dry-run"


class ReclaimCollapseError(RuntimeError):
    """Base error for the offline reclaim-collapse engine."""


class ShadowSafetyError(ReclaimCollapseError):
    """The target is not an explicitly disposable, offline shadow clone."""


@dataclass(frozen=True)
class CollapseResult:
    """Observable result of one offline collapse attempt.

    ``ledger_entries_rewritten`` counts ledger entries changed in the SNAPSHOT blobs the
    collapse actually wrote; a SNAPSHOT carried unchanged into later commits is not
    counted again.
    """

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
    used_fast_import: bool
    events_resigned: int = 0
    ledger_entries_resigned: int = 0

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
    resign: ResignPlan | None = None,
) -> CollapseResult:
    """Collapse history at ``boundary_commit`` inside a disposable shadow tracker.

    The target ref is only updated when ``apply`` is true. Dry-runs still exercise the
    core-git fast-import path against a temporary ref, then delete that ref; HEAD is left
    unchanged.

    ``resign`` (from :func:`rebar._commands.reclaim_resign.build_resign_plan`) substitutes
    re-signed bytes for failing events; it must have been built at this branch tip.
    """

    tracker = Path(shadow_tracker).resolve()
    _assert_shadow_tracker(tracker)
    boundary = _rev_parse(tracker, boundary_commit)
    original_tip = _rev_parse(tracker, branch)
    _check_resign_tip(resign, original_tip)
    if not _is_ancestor(tracker, boundary, original_tip):
        raise ReclaimCollapseError(f"boundary {boundary} is not an ancestor of {original_tip}")

    _require_remote_horizon_ref(tracker)
    if not _remote_horizon_eligible(tracker, boundary, now=now):
        raise ReclaimCollapseError(
            f"boundary {boundary} is not eligible under the remote-anchored reclaim horizon"
        )

    original_position_map = _position_commit_map_for_tracker(tracker)
    ref = _SCRATCH_REF if apply else _DRY_RUN_REF
    _delete_ref(tracker, ref)
    rewrite = _StreamedRewrite(tracker, ref, boundary, original_position_map, resign)
    try:
        rewrite.run(_rev_list_with_parents(tracker, f"{boundary}..{original_tip}"))
        if resign is not None:
            resign.assert_applied()
    except StreamError as exc:
        _delete_ref(tracker, ref)
        raise ReclaimCollapseError(str(exc)) from exc
    except BaseException:
        _delete_ref(tracker, ref)
        raise
    finally:
        rewrite.close_reader()

    original_count = int(_git(tracker, "rev-list", "--count", original_tip).stdout)
    collapsed = max(0, original_count - rewrite.replayed)
    if apply:
        _update_current_branch(tracker, rewrite.current_tip, original_tip)
        # raw-git-ok: resets only the safety-checked shadow clone worktree
        _git(
            tracker,
            "reset",
            "--hard",
            "-q",
            rewrite.current_tip,
        )
    _delete_ref(tracker, ref)

    return CollapseResult(
        applied=apply,
        original_tip=original_tip,
        boundary_commit=boundary,
        checkpoint_sha=rewrite.checkpoint_sha,
        ledger_anchor_sha=rewrite.ledger_anchor_sha,
        rewritten_tip=rewrite.current_tip,
        enforce_since=rewrite.ledger_anchor_sha,
        collapsed_commits=collapsed,
        replayed_commits=rewrite.replayed,
        ledger_entries_rewritten=rewrite.ledger_rewrites,
        used_fast_import=rewrite.commits_written > 0,
        events_resigned=len(resign.substituted) if resign else 0,
        ledger_entries_resigned=len(resign.resigned_ledger) if resign else 0,
    )


def _check_resign_tip(resign: ResignPlan | None, original_tip: str) -> None:
    if resign is not None and resign.tip != original_tip:
        from rebar._commands.reclaim_resign import ResignError

        raise ResignError(f"re-sign plan was built at {resign.tip}, but the tip is {original_tip}")


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
    except (config.ConfigError, OSError, RuntimeError) as exc:
        raise ShadowSafetyError(
            f"refusing to rewrite {tracker}: cannot resolve live configured tracker"
        ) from exc


def _remote_horizon_eligible(tracker: Path, commit: str, *, now: datetime | None) -> bool:
    from rebar._store.reclaim_eligibility import checkpoint_commit_eligible

    try:
        return checkpoint_commit_eligible(str(tracker), commit, now=now)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ReclaimCollapseError("cannot evaluate remote-anchored reclaim horizon") from exc


def _require_remote_horizon_ref(tracker: Path) -> None:
    try:
        from rebar import config

        remote = config.tickets_remote(tracker)
        branch = config.tickets_branch(tracker)
    except (config.ConfigError, OSError, RuntimeError) as exc:
        raise ReclaimCollapseError(
            "cannot resolve reclaim horizon configuration for shadow tracker"
        ) from exc
    ref = f"refs/remotes/{remote}/{branch}"
    if _git(tracker, "show-ref", "--verify", "--quiet", ref, check=False).returncode != 0:
        raise ReclaimCollapseError(f"shadow tracker is missing required remote horizon ref {ref}")


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


def _commit_message(tracker: Path, commit: str) -> bytes:
    raw = _git(  # raw-git-ok: cat-file commit reads an object type, not git-commit mutation
        tracker, "cat-file", "commit", commit, text=False
    ).stdout
    _headers, sep, message = raw.partition(b"\n\n")
    if not sep:
        raise ReclaimCollapseError(f"cannot read commit message for {commit}")
    return message


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
    below: AbstractSet[str],
    resign: ResignPlan | None = None,
) -> tuple[bytes | None, int]:
    try:
        event = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, 0
    if event.get("event_type") != "SNAPSHOT":
        return None, 0
    count = _rewrite_ledger_in_event(event, original_position_map, old_to_new, boundary, below)
    ledger = event.get("data", {}).get("compiled_state", {}).get("authorship_ledger")
    if resign is not None and isinstance(ledger, list):
        count += resign.resign_ledger(ledger)
    if count == 0:
        return None, 0
    return json.dumps(event, sort_keys=True, separators=(",", ":")).encode() + b"\n", count


def _rewrite_ledger_in_event(
    event: dict,
    original_position_map: Mapping[str, str],
    old_to_new: Mapping[str, str],
    boundary: str,
    below: AbstractSet[str],
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
        new_commit = _map_old_commit(old_commit, boundary, old_to_new, below)
        if new_commit and pos.get("commit_sha") != new_commit:
            pos["commit_sha"] = new_commit
            changed += 1
    return changed


def _map_old_commit(
    old_commit: object, boundary: str, old_to_new: Mapping[str, str], below: AbstractSet[str]
) -> str | None:
    """Map an original commit to its rewrite; ``below`` is every ancestor of the boundary."""
    if not isinstance(old_commit, str) or not old_commit:
        return None
    if old_commit in old_to_new:
        return old_to_new[old_commit]
    if old_commit in below:
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


def _rev_list_with_parents(tracker: Path, rev_range: str) -> list[tuple[str, list[str]]]:
    """Commits of *rev_range* in replay order, each with its original parents."""
    out = _git(tracker, "rev-list", "--reverse", "--topo-order", "--parents", rev_range).stdout
    rows: list[tuple[str, list[str]]] = []
    for line in out.splitlines():
        shas = line.split()
        if shas:
            rows.append((shas[0], shas[1:]))
    return rows


def _is_snapshot_candidate(path: bytes) -> bool:
    return b"-SNAPSHOT.json" in path.rsplit(b"/", 1)[-1]


class _StreamedRewrite:
    """Write the checkpoint, the ledger anchor and every replayed commit in one stream.

    The output equals the former per-commit writer's: the same parents, metadata and
    trees. Each replayed commit applies only the paths its original commit changed
    relative to the source of its rewritten first parent, plus inline overrides for the
    SNAPSHOTs whose ledger rewrite differs from what that parent already carries.
    """

    def __init__(
        self,
        tracker: Path,
        ref: str,
        boundary: str,
        position_map: Mapping[str, str],
        resign: ResignPlan | None = None,
    ) -> None:
        self.tracker = tracker
        self.resign = resign
        self.ref = ref
        self.boundary = boundary
        self.position_map = position_map
        self.below = frozenset(_git(tracker, "rev-list", boundary).stdout.split())
        self.reader = ObjectReader(tracker)
        self.old_to_new: dict[str, str] = {}
        self.map_index: dict[str, int] = {}
        self.source_of: dict[str, str] = {}
        self.k_of: dict[str, int] = {}
        self.snapmaps: dict[str, dict[bytes, tuple[str, str]]] = {}
        self.refs_of: dict[str, frozenset[str] | None] = {}
        self.ledger_rewrites = 0
        self.replayed = 0
        self.commits_written = 0
        self.checkpoint_sha = ""
        self.ledger_anchor_sha = ""
        self.current_tip = ""

    def close_reader(self) -> None:
        self.reader.close()

    # raw-git-ok: streams commits only into the safety-checked disposable shadow clone
    def run(self, commits: list[tuple[str, list[str]]]) -> None:
        session = FastImportSession(self.tracker)
        try:
            self._write_base(session)
            in_range = {commit for commit, _ in commits}
            for commit, parents in commits:
                self._replay(session, commit, parents, in_range)
            session.close()
        except BaseException:
            session.abort()
            raise
        self.commits_written = session.commits_written

    def _write_base(self, session: FastImportSession) -> None:
        boundary = self.boundary
        author, committer, message = self._raw_commit(boundary)
        base_tree = b"M 040000 " + self._tree_of(boundary).encode() + b' ""\n'
        # The checkpoint and the ledger anchor both re-emit the boundary tree, so both carry
        # the re-signed raw events; the anchor is the base of every replayed commit.
        raw_overrides = self._raw_overrides(boundary)
        self.checkpoint_sha = session.commit(
            self.ref,
            author=author,
            committer=committer,
            message=message,
            parents=(),
            lines=[base_tree, *raw_overrides],
        )
        snapmap = self._snapshot_paths(boundary)
        self.source_of[self.checkpoint_sha] = boundary
        self.k_of[self.checkpoint_sha] = -1
        self.snapmaps[self.checkpoint_sha] = snapmap
        self._map(boundary, self.checkpoint_sha)

        derived = _derived_identity()
        lines = [base_tree, *raw_overrides]
        anchor_rewrites = 0
        for path, (mode, oid) in snapmap.items():
            rewritten, count = self._rewritten(oid)
            if rewritten is not None:
                anchor_rewrites += count
                lines.append(_inline(mode, path, rewritten))
        self.ledger_anchor_sha = session.commit(
            self.ref,
            author=derived,
            committer=derived,
            message=b"rebar reclaim: reconcile authorship ledger\n",
            parents=(self.checkpoint_sha,),
            lines=lines,
        )
        self.ledger_rewrites += anchor_rewrites
        self.source_of[self.ledger_anchor_sha] = boundary
        self.k_of[self.ledger_anchor_sha] = 0
        self.snapmaps[self.ledger_anchor_sha] = snapmap
        self.current_tip = self.ledger_anchor_sha

    def _replay(
        self, session: FastImportSession, commit: str, parents: list[str], in_range: set[str]
    ) -> None:
        new_parents: list[str] = []
        for parent in parents:
            # The boundary's rewrite for ledger purposes is the checkpoint, but its place in
            # the graph belongs to the ledger anchor, so enforce_since stays on the branch.
            if parent == self.boundary:
                mapped: str | None = self.ledger_anchor_sha
            else:
                mapped = self.old_to_new.get(parent)
            if mapped is None and parent not in in_range:
                mapped = self.current_tip
            if mapped is not None and mapped not in new_parents:
                new_parents.append(mapped)
        if not new_parents:
            new_parents = [self.current_tip]
        first = new_parents[0]
        snapmap = dict(self.snapmaps[first])
        lines, changed = self._diff_lines(self.source_of[first], commit, snapmap)
        k_first = self.k_of[first]
        for path, (mode, oid) in snapmap.items():
            if path not in changed and self._latest(oid) <= k_first:
                continue
            rewritten, count = self._rewritten(oid)
            if rewritten is not None:
                self.ledger_rewrites += count
                lines.append(_inline(mode, path, rewritten))
            elif path not in changed:
                lines.append(f"M {mode} {oid} ".encode() + _quote_path(path) + b"\n")
        author, committer, message = self._raw_commit(commit)
        new = session.commit(
            self.ref,
            author=author,
            committer=committer,
            message=message,
            parents=new_parents,
            lines=lines,
        )
        self.source_of[new] = commit
        self.k_of[new] = len(self.map_index) - 1
        self.snapmaps[new] = snapmap
        self._map(commit, new)
        self.current_tip = new
        self.replayed += 1

    def _diff_lines(
        self, source: str, commit: str, snapmap: dict[bytes, tuple[str, str]]
    ) -> tuple[list[bytes], set[bytes]]:
        out = _git(
            self.tracker,
            "diff-tree",
            "-r",
            "-z",
            "--raw",
            "--no-renames",
            source,
            commit,
            text=False,
        ).stdout
        fields = out.split(b"\x00")
        lines: list[bytes] = []
        changed: set[bytes] = set()
        i = 0
        while i + 1 < len(fields) and fields[i]:
            meta, path = fields[i], fields[i + 1]
            i += 2
            _old_mode, new_mode, _old_oid, new_oid, status = meta.lstrip(b":").split()
            changed.add(path)
            if status.startswith(b"D"):
                lines.append(b"D " + _quote_path(path) + b"\n")
                snapmap.pop(path, None)
                continue
            mode, oid = new_mode.decode(), new_oid.decode()
            lines.append(self._m_line(mode, oid, path))
            if _is_snapshot_candidate(path) and self._is_snapshot(oid):
                snapmap[path] = (mode, oid)
            else:
                snapmap.pop(path, None)
        return lines, changed

    def _snapshot_paths(self, commit: str) -> dict[bytes, tuple[str, str]]:
        out = _git(self.tracker, "ls-tree", "-rz", commit, text=False).stdout
        snapmap: dict[bytes, tuple[str, str]] = {}
        for raw in out.split(b"\x00"):
            if not raw:
                continue
            meta, path = raw.split(b"\t", 1)
            mode, kind, oid = meta.decode().split()
            if kind == "blob" and _is_snapshot_candidate(path) and self._is_snapshot(oid):
                snapmap[path] = (mode, oid)
        return snapmap

    def _is_snapshot(self, oid: str) -> bool:
        return self._refs(oid) is not None

    def _refs(self, oid: str) -> frozenset[str] | None:
        """The original commits a SNAPSHOT blob's ledger references (``None``: not one)."""
        if oid in self.refs_of:
            return self.refs_of[oid]
        try:
            event = json.loads(self.reader.read(oid).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            event = None
        if not isinstance(event, dict) or event.get("event_type") != "SNAPSHOT":
            self.refs_of[oid] = None
            return None
        ledger = event.get("data", {}).get("compiled_state", {}).get("authorship_ledger")
        refs: set[str] = set()
        for entry in ledger if isinstance(ledger, list) else []:
            pos = entry.get("position") if isinstance(entry, dict) else None
            if not isinstance(pos, dict):
                continue
            position = pos.get("position")
            if isinstance(position, str):
                old = self.position_map.get(position) or pos.get("commit_sha")
                if isinstance(old, str) and old:
                    refs.add(self.boundary if old in self.below else old)
        self.refs_of[oid] = frozenset(refs)
        return self.refs_of[oid]

    def _latest(self, oid: str) -> int:
        refs = self._refs(oid) or frozenset()
        return max((self.map_index[r] for r in refs if r in self.map_index), default=-1)

    def _rewritten(self, oid: str) -> tuple[bytes | None, int]:
        return _rewrite_snapshot_blob(
            self.reader.read(oid),
            self.position_map,
            self.old_to_new,
            self.boundary,
            self.below,
            self.resign,
        )

    def _m_line(self, mode: str, oid: str, path: bytes) -> bytes:
        """A file command for ``path``: re-signed bytes inline for a raw target."""
        data = self.resign.raw_bytes(oid) if self.resign is not None else None
        if data is not None:
            return _inline(mode, path, data)
        return f"M {mode} {oid} ".encode() + _quote_path(path) + b"\n"

    def _raw_overrides(self, commit: str) -> list[bytes]:
        if self.resign is None or not self.resign.raw_blobs:
            return []
        out = _git(self.tracker, "ls-tree", "-rz", commit, text=False).stdout
        lines: list[bytes] = []
        for raw in out.split(b"\x00"):
            if not raw:
                continue
            meta, path = raw.split(b"\t", 1)
            mode, kind, oid = meta.decode().split()
            if kind == "blob" and oid in self.resign.raw_blobs:
                lines.append(self._m_line(mode, oid, path))
        return lines

    def _map(self, old: str, new: str) -> None:
        self.old_to_new[old] = new
        self.map_index[old] = len(self.map_index)

    def _tree_of(self, commit: str) -> str:
        return _git(self.tracker, "rev-parse", f"{commit}^{{tree}}").stdout.strip()

    def _raw_commit(self, commit: str) -> tuple[bytes, bytes, bytes]:
        raw = self.reader.read(commit)
        headers, sep, message = raw.partition(b"\n\n")
        if not sep:
            raise ReclaimCollapseError(f"cannot read commit message for {commit}")
        author = committer = b""
        for line in headers.split(b"\n"):
            if line.startswith(b"author "):
                author = line[len(b"author ") :]
            elif line.startswith(b"committer "):
                committer = line[len(b"committer ") :]
        if not author or not committer:
            raise ReclaimCollapseError(f"cannot read commit metadata for {commit}")
        return author, committer, message


def _inline(mode: str, path: bytes, data: bytes) -> bytes:
    return (
        f"M {mode} inline ".encode()
        + _quote_path(path)
        + b"\n"
        + f"data {len(data)}\n".encode()
        + data
        + b"\n"
    )


def _derived_identity() -> bytes:
    an, ae, ad, _cn, _ce, _cd = _derived_metadata()
    return f"{an} <{ae}> {ad}".encode()
