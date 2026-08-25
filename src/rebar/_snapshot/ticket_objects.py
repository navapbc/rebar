"""Git-object and resolver-index leaf for the lazy pinned ticket view."""

from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from rebar._snapshot.ticket_receipt import TicketsOID, _run_git

_JIRA_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]+-[0-9]+$")
_SHORT_RE = re.compile(r"^[0-9a-f]{4}-[0-9a-f]{4}$")
_RESOLVER_SCHEMA = 2
_RESOLVER_CACHE_MAX = 2


class _ResolverIndex:
    def __init__(
        self,
        *,
        ticket_ids: tuple[str, ...],
        aliases: Mapping[str, tuple[str, ...]],
        alias_sources: Mapping[str, tuple[str, ...]],
        jira_reverse: Mapping[str, str],
    ) -> None:
        self.ticket_ids = ticket_ids
        self.aliases = aliases
        self.alias_sources = alias_sources
        self.jira_reverse = jira_reverse


_resolver_cache: OrderedDict[tuple[str, str, int], _ResolverIndex] = OrderedDict()
_resolver_cache_lock = threading.Lock()


def _error(message: str) -> Exception:
    from rebar._snapshot.ticket_view import PinnedTicketViewError

    return PinnedTicketViewError(message)


def _validate_tree_path(path: str) -> None:
    pure = PurePosixPath(path)
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or "\n" in path
        or "\r" in path
        or any(part in ("", ".", "..") for part in pure.parts)
        or pure.as_posix() != path
    ):
        raise _error(f"unsafe path in pinned ticket tree: {path!r}")


def _json_object(blob: bytes, path: str) -> dict[str, object]:
    try:
        value = json.loads(blob)
    except (TypeError, ValueError) as exc:
        raise _error(f"corrupt JSON in pinned ticket object {path!r}: {exc}") from None
    if not isinstance(value, dict):
        raise _error(f"pinned ticket object {path!r} is not a JSON object")
    return value


class TicketObjectStore:
    """Demand reader for one immutable ticket-store tree plus its bounded resolver cache."""

    def __init__(self, tracker: str, tickets_oid: TicketsOID, metrics: dict[str, int]):
        self.tracker = str(Path(tracker).resolve())
        self.tickets_oid = tickets_oid
        self.metrics = metrics
        self._paths: tuple[str, ...] | None = None
        self._path_set: frozenset[str] | None = None
        self._ticket_ids: tuple[str, ...] | None = None
        self._raw_link_sources: dict[str, tuple[str, ...]] | None = None

    def tree_paths(self) -> tuple[str, ...]:
        if self._paths is None:
            started = time.monotonic_ns()
            raw = _run_git(
                self.tracker, "ls-tree", "-r", "-z", "--full-tree", self.tickets_oid.value
            ).stdout
            paths: list[str] = []
            for record in raw.split(b"\0"):
                if not record:
                    continue
                try:
                    metadata, encoded_path = record.split(b"\t", 1)
                    mode, object_type, _oid = metadata.split(b" ", 2)
                except ValueError:
                    raise _error(f"malformed git ls-tree record: {record!r}") from None
                path = encoded_path.decode(errors="surrogateescape")
                _validate_tree_path(path)
                if object_type != b"blob" or mode not in (b"100644", b"100755"):
                    raise _error(
                        f"unsupported object mode in pinned ticket tree: {mode!r} {path!r}"
                    )
                paths.append(path)
            self._paths = tuple(paths)
            self._path_set = frozenset(paths)
            self.metrics["ticket_object_list_ms"] += (time.monotonic_ns() - started) // 1_000_000
        return self._paths

    @staticmethod
    def is_root_event_path(path: str) -> bool:
        if path.startswith(".") or path.count("/") != 1:
            return False
        name = path.rsplit("/", 1)[1]
        if name.startswith(".") or not name.endswith(".json"):
            return False
        from rebar.reducer._cache import is_active_event

        return is_active_event(name)

    def ticket_event_paths(self, ticket_id: str) -> tuple[str, ...]:
        prefix = f"{ticket_id}/"
        return tuple(
            path
            for path in self.tree_paths()
            if path.startswith(prefix) and self.is_root_event_path(path)
        )

    def ticket_ids(self) -> tuple[str, ...]:
        if self._ticket_ids is None:
            ids = {
                path.split("/", 1)[0]
                for path in self.tree_paths()
                if self.is_root_event_path(path)
                and (path.endswith("-CREATE.json") or path.endswith("-SNAPSHOT.json"))
            }
            self._ticket_ids = tuple(sorted(ids))
        return self._ticket_ids

    def cat_files(self, paths: Iterable[str]) -> dict[str, bytes]:
        requested = tuple(dict.fromkeys(paths))
        if not requested:
            return {}
        started = time.monotonic_ns()
        for path in requested:
            _validate_tree_path(path)
        specs = b"".join(
            f"{self.tickets_oid.value}:{path}\n".encode(errors="surrogateescape")
            for path in requested
        )
        out = _run_git(self.tracker, "cat-file", "--batch", input_bytes=specs).stdout
        result: dict[str, bytes] = {}
        pos = 0
        for path in requested:
            newline = out.find(b"\n", pos)
            if newline < 0:
                raise _error("truncated git cat-file batch header")
            header = out[pos:newline]
            pos = newline + 1
            if header.endswith(b" missing"):
                if self._path_set is None:
                    self.tree_paths()
                if path in (self._path_set or ()):
                    raise _error(f"missing Git object for pinned ticket path {path!r}")
                continue
            parts = header.rsplit(b" ", 2)
            if len(parts) != 3 or parts[1] != b"blob":
                raise _error(f"unexpected git object header: {header!r}")
            try:
                size = int(parts[2])
            except ValueError:
                raise _error(f"invalid git object size in header: {header!r}") from None
            if pos + size >= len(out) or out[pos + size : pos + size + 1] != b"\n":
                raise _error("truncated git cat-file batch payload")
            result[path] = out[pos : pos + size]
            pos += size + 1
        self.metrics["ticket_object_read_ms"] += (time.monotonic_ns() - started) // 1_000_000
        self.metrics["ticket_object_reads"] += len(result)
        return result

    def _resolver_event_paths(
        self, known_ids: frozenset[str]
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Locate each ticket's alias-bearing CREATE and latest SNAPSHOT objects."""
        creates: dict[str, str] = {}
        snapshots: dict[str, str] = {}
        for path in self.tree_paths():
            if not self.is_root_event_path(path):
                continue
            ticket_id = path.split("/", 1)[0]
            if ticket_id not in known_ids:
                continue
            if path.endswith("-CREATE.json") and ticket_id not in creates:
                creates[ticket_id] = path
            elif path.endswith("-SNAPSHOT.json") and not path.endswith(
                "-PRECONDITIONS-SNAPSHOT.json"
            ):
                snapshots[ticket_id] = path
        return creates, snapshots

    def _resolver_alias_maps(
        self,
        ids: tuple[str, ...],
        creates: Mapping[str, str],
        snapshots: Mapping[str, str],
        blobs: Mapping[str, bytes],
    ) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
        """Resolve stored or deterministic aliases and record every consulted object."""
        from rebar._alias import compute_alias

        aliases: dict[str, list[str]] = {}
        alias_sources: dict[str, tuple[str, ...]] = {}
        for ticket_id in ids:
            stored = ""
            consulted: list[str] = []
            create_path = creates.get(ticket_id)
            snapshot_path = snapshots.get(ticket_id)
            if create_path:
                consulted.append(create_path)
                payload = _json_object(blobs[create_path], create_path)
                data = payload.get("data", {}) or {}
                if not isinstance(data, Mapping):
                    raise _error(f"resolver source {create_path!r} has non-object event data")
                stored = data.get("alias") or ""
            if not stored and snapshot_path:
                consulted.append(snapshot_path)
                payload = _json_object(blobs[snapshot_path], snapshot_path)
                data = payload.get("data", {}) or {}
                if not isinstance(data, Mapping):
                    raise _error(f"resolver source {snapshot_path!r} has non-object event data")
                compiled = data.get("compiled_state", {}) or {}
                if not isinstance(compiled, Mapping):
                    raise _error(f"resolver source {snapshot_path!r} has non-object compiled_state")
                stored = compiled.get("alias") or ""
            if stored and not isinstance(stored, str):
                raise _error(f"resolver sources for {ticket_id!r} contain a non-string alias")
            alias = stored or compute_alias(ticket_id) or ""
            if alias:
                aliases.setdefault(alias, []).append(ticket_id)
            alias_sources[ticket_id] = tuple(consulted)
        return (
            {alias: tuple(sorted(value)) for alias, value in aliases.items()},
            alias_sources,
        )

    def _resolver_index(self) -> _ResolverIndex:
        cache_key = (self.tracker, self.tickets_oid.value, _RESOLVER_SCHEMA)
        with _resolver_cache_lock:
            cached = _resolver_cache.get(cache_key)
            if cached is not None:
                _resolver_cache.move_to_end(cache_key)
                return cached
        ids = self.ticket_ids()
        creates, snapshots = self._resolver_event_paths(frozenset(ids))
        blobs = self.cat_files(
            path
            for ticket_id in ids
            for path in (creates.get(ticket_id), snapshots.get(ticket_id))
            if path
        )
        aliases, alias_sources = self._resolver_alias_maps(ids, creates, snapshots, blobs)
        binding_path = ".bridge_state/bindings.json"
        binding_blob = self.cat_files([binding_path]).get(binding_path, b"{}")
        reverse = _json_object(binding_blob, binding_path).get("reverse", {}) or {}
        if not isinstance(reverse, Mapping):
            raise _error(f"pinned resolver bindings {binding_path!r} has non-object reverse map")
        index = _ResolverIndex(
            ticket_ids=ids,
            aliases=aliases,
            alias_sources=alias_sources,
            jira_reverse={
                str(jira_key): str(value)
                for jira_key, value in reverse.items()
                if isinstance(jira_key, str) and isinstance(value, str)
            },
        )
        with _resolver_cache_lock:
            _resolver_cache[cache_key] = index
            _resolver_cache.move_to_end(cache_key)
            while len(_resolver_cache) > _RESOLVER_CACHE_MAX:
                _resolver_cache.popitem(last=False)
        return index

    def resolve(self, ticket_ref: str) -> tuple[str | None, str]:
        ref = str(ticket_ref)
        ids = self.ticket_ids()
        kind = "exact"
        resolved: str | None = ref if ref in ids else None
        if resolved is not None:
            return resolved, kind
        if resolved is None and _SHORT_RE.fullmatch(ref):
            kind = "short"
            matches = [tid for tid in ids if tid.startswith(ref)]
            resolved = matches[0] if len(matches) == 1 else None
            return resolved, kind
        if resolved is None and _JIRA_RE.fullmatch(ref):
            kind = "jira_binding"
            reverse = self._resolver_index().jira_reverse
            resolved = reverse.get(ref) or reverse.get(ref.upper())
            if resolved not in ids:
                resolved = None
        if resolved is None:
            kind = "alias"
            alias_matches = self._resolver_index().aliases.get(ref, ())
            if alias_matches:
                return (alias_matches[0] if len(alias_matches) == 1 else None), kind
            if len(ref) >= 4:
                kind = "prefix"
                prefix = [tid for tid in ids if tid.startswith(ref)]
                resolved = prefix[0] if len(prefix) == 1 else None
        return resolved, kind

    def resolver_material(self, raw: str) -> tuple[tuple[str, ...], dict[str, bytes]]:
        """Return the minimal filesystem inputs that reproduce live resolver semantics."""
        index = self._resolver_index()
        known = frozenset(index.ticket_ids)
        directories: set[str] = set()
        paths: list[str] = []
        if raw in known:
            directories.add(raw)
            return tuple(sorted(directories)), {}
        if _SHORT_RE.fullmatch(raw):
            directories.update(tid for tid in index.ticket_ids if tid.startswith(raw))
            return tuple(sorted(directories)), {}
        if _JIRA_RE.fullmatch(raw):
            paths.append(".bridge_state/bindings.json")
            bound = index.jira_reverse.get(raw) or index.jira_reverse.get(raw.upper())
            if bound in known:
                directories.add(str(bound))
                return tuple(sorted(directories)), self.cat_files(paths)
        aliases = index.aliases.get(raw, ())
        if aliases:
            directories.update(aliases)
            for ticket_id in aliases:
                paths.extend(index.alias_sources.get(ticket_id, ()))
        elif len(raw) >= 4:
            directories.update(tid for tid in index.ticket_ids if tid.startswith(raw))
        return tuple(sorted(directories)), self.cat_files(paths)

    def grep_ticket_ids(self, needle: str) -> tuple[str, ...]:
        proc = _run_git(
            self.tracker,
            "grep",
            "-l",
            "-F",
            needle,
            self.tickets_oid.value,
            "--",
            check=False,
        )
        if proc.returncode not in (0, 1):
            detail = proc.stderr.decode(errors="replace").strip()
            raise _error(f"git grep failed: {detail}")
        ids: set[str] = set()
        known = frozenset(self.ticket_ids())
        for line in proc.stdout.decode(errors="surrogateescape").splitlines():
            path = line.split(":", 1)[-1]
            if "/" in path and path.split("/", 1)[0] in known:
                ids.add(path.split("/", 1)[0])
        return tuple(sorted(ids))

    def inbound_candidate_ids(self, canonical: str) -> tuple[str, ...]:
        """Return every ticket that may reduce to an inbound edge for ``canonical``.

        Grepping the canonical id covers normal events and compacted snapshots. Legacy LINK
        events may instead store an alias, short id, or Jira key, so lazily index active LINK
        payloads by their pinned resolver result and union those source tickets here.
        """
        if self._raw_link_sources is None:
            by_target: dict[str, set[str]] = {}
            paths = [
                path
                for path in self.tree_paths()
                if self.is_root_event_path(path) and path.endswith("-LINK.json")
            ]
            for path, blob in self.cat_files(paths).items():
                event = _json_object(blob, path)
                data = event.get("data", {}) or {}
                if not isinstance(data, Mapping):
                    raise _error(f"pinned LINK object {path!r} has non-object event data")
                raw = str(data.get("target_id", data.get("target", "")) or "")
                resolved, _kind = self.resolve(raw) if raw else (None, "")
                if resolved is not None:
                    by_target.setdefault(resolved, set()).add(path.split("/", 1)[0])
            self._raw_link_sources = {
                target: tuple(sorted(sources)) for target, sources in by_target.items()
            }
        candidates = set(self.grep_ticket_ids(canonical))
        candidates.update(self._raw_link_sources.get(canonical, ()))
        return tuple(sorted(candidates))

    def event_payloads(self, canonical: str, event_type: str) -> list[dict[str, object]]:
        wanted = str(event_type).upper()
        paths = [
            path for path in self.ticket_event_paths(canonical) if path.endswith(f"-{wanted}.json")
        ]
        payloads: list[dict[str, object]] = []
        for path, blob in self.cat_files(paths).items():
            event = _json_object(blob, path)
            data = event.get("data") if isinstance(event, dict) else None
            if str(event.get("event_type", "")).upper() == wanted and isinstance(data, dict):
                payloads.append(dict(data))
        return payloads


__all__ = ["TicketObjectStore"]
