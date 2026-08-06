"""Committed store-compatibility record + the fail-closed capability gate (story 21dd).

A v1.0 rebar refuses to *mutate or externally publish* a store whose committed
``.store-compat.json`` record it cannot interpret, while keeping **reads and
diagnostics available**. The record is a small JSON object committed on the tickets
branch::

    {"format_version": <int>, "required_capabilities": [<str>, ...]}

Four record states (the whole contract):

1. **ABSENT** → implicit legacy (format version ``0``): compatible, **passes
   through** — a legacy store predating this feature is never blocked.
2. **PRESENT + compatible** → the ``format_version`` is known and every declared
   ``required_capabilities`` entry is one this binary provides → pass.
3. **PRESENT + incompatible** → an unrecognized ``format_version``, or a
   ``required_capabilities`` entry not in :data:`KNOWN_CAPABILITIES` → the store was
   written by a NEWER rebar → :class:`StoreIncompatibleError` (fail CLOSED).
4. **PRESENT + corrupt/unreadable** → a JSON parse error, a malformed shape, a
   truncation, or a read error (permission denied) → :class:`StoreIncompatibleError`
   naming the parse error + record path. A corrupt record is **NEVER** silently
   treated as absent (that would let it bypass the gate) — it fails CLOSED.

**Leaf module — stdlib + :mod:`rebar._store.fsutil` ONLY, no other ``rebar.*``
imports.** :mod:`rebar._store.lock` imports :func:`check_store_compat` to run the
gate inside ``acquire()``; a ``rebar._commands.*`` / ``rebar.config`` import here
would create an import cycle. Keep it leaf.
"""

from __future__ import annotations

import json
import os

from rebar._store import fsutil
from rebar._store.gitutil import run_git

__all__ = [
    "COMPAT_FILENAME",
    "CURRENT_FORMAT_VERSION",
    "KNOWN_CAPABILITIES",
    "KNOWN_FORMAT_VERSIONS",
    "StoreIncompatibleError",
    "check_store_compat",
    "describe_store_compat",
    "store_epoch_merge_target",
    "store_epoch_problem",
    "write_compat_record",
]

# The store format this v1.0 rebar writes and reads. ``0`` is the implicit legacy
# version an ABSENT record stands for (a pre-feature store), so both are known.
CURRENT_FORMAT_VERSION = 1
KNOWN_FORMAT_VERSIONS: frozenset[int] = frozenset({0, 1})

# Capabilities this binary provides. v1.0 introduces the record itself but declares
# no named capability yet, so the set is empty: any capability a record *requires* is
# by definition unknown to v1.0 and fails the gate closed (a forward-compat guard —
# a newer rebar that adds a capability will list it here so its stores validate).
KNOWN_CAPABILITIES: frozenset[str] = frozenset()

# The committed record's filename, under the tracker root (tickets-branch worktree).
COMPAT_FILENAME = ".store-compat.json"


class StoreIncompatibleError(Exception):
    """The store's committed ``.store-compat.json`` cannot be interpreted by this
    rebar (unknown format version / capability, or a corrupt/unreadable record), so a
    mutating or externally-publishing operation must fail CLOSED before any side
    effect. Carries a non-zero ``returncode`` (mirrors ``LockTimeout``/``RebaseGuard``
    in :mod:`rebar._store.lock`) so a CLI seam surfaces a distinct exit code."""

    returncode = 78

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _record_path(tracker: str | os.PathLike[str]) -> str:
    return os.path.join(os.fspath(tracker), COMPAT_FILENAME)


def _epoch_from_record(raw: str, source: str) -> tuple[str | None, str | None]:
    """Return a record's optional epoch, or a fail-closed diagnostic.

    The epoch guard needs a stricter view than :func:`_analyze`: ``epoch`` is an
    additive key there so old binaries must ignore it, while a binary that knows
    the guard must reject a malformed epoch record rather than mistake it for a
    pre-epoch store.
    """
    try:
        record = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as err:
        return None, f"store-compat record at {source} is not valid JSON: {err}"
    if not isinstance(record, dict):
        return None, f"store-compat record at {source} is malformed: expected a JSON object"

    version = record.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool):
        return None, (
            f"store-compat record at {source} is malformed: 'format_version' must be an integer"
        )
    caps = record.get("required_capabilities")
    if not isinstance(caps, list) or not all(isinstance(cap, str) for cap in caps):
        return None, (
            f"store-compat record at {source} is malformed: 'required_capabilities' "
            "must be a list of strings"
        )

    if "epoch" not in record:
        return None, None
    epoch = record["epoch"]
    if not isinstance(epoch, str):
        return None, f"store-compat record at {source} is malformed: 'epoch' must be a string"
    return epoch, None


def _epoch_refusal(detail: str) -> str:
    return (
        "tickets store epoch guard refused the operation: "
        f"{detail}; re-clone the tracker before retrying"
    )


def _local_store_epoch(tracker: str | os.PathLike[str]) -> tuple[str | None, str | None]:
    path = _record_path(tracker)
    try:
        with open(path, encoding="utf-8") as fh:
            epoch, problem = _epoch_from_record(fh.read(), path)
            return epoch, f"local {problem}" if problem is not None else None
    except FileNotFoundError:
        return None, None
    except OSError as err:
        return None, f"local store-compat record at {path} is unreadable: {err}"


def _remote_store_epoch(
    tracker: str | os.PathLike[str], remote_ref: str
) -> tuple[str | None, str | None]:
    """Read the remote-side record directly from its Git tree, not the worktree."""
    tracker_path = os.fspath(tracker)
    source = f"{remote_ref}:{COMPAT_FILENAME}"
    try:
        shown = run_git(tracker_path, "show", source, check=False)
    except OSError as err:
        return None, f"remote ref {remote_ref} is unreadable: {err}"
    if shown.returncode == 0:
        epoch, problem = _epoch_from_record(shown.stdout, source)
        return epoch, f"remote {problem}" if problem is not None else None

    # `git show ref:path` uses the same non-zero status for a missing path and an
    # unreadable ref. Resolve the ref separately so operators get the right remedy.
    try:
        resolved = run_git(
            tracker_path, "rev-parse", "--verify", f"{remote_ref}^{{commit}}", check=False
        )
    except OSError as err:
        return None, f"remote ref {remote_ref} is unreadable: {err}"
    if resolved.returncode != 0:
        return None, f"remote ref {remote_ref} is unreadable"
    return None, None  # The readable remote tree simply predates the record.


def store_epoch_problem(tracker: str | os.PathLike[str], remote_ref: str) -> str | None:
    """Return a refusal diagnostic when a union would cross incompatible epochs.

    This is intentionally non-raising: sync and push are best-effort paths. An
    absent record and a present record with no epoch both describe the pre-reclaim
    store and are compatible; otherwise epochs must be equal opaque strings.
    """
    local_epoch, local_problem = _local_store_epoch(tracker)
    if local_problem is not None:
        return _epoch_refusal(local_problem)
    remote_epoch, remote_problem = _remote_store_epoch(tracker, remote_ref)
    if remote_problem is not None:
        return _epoch_refusal(remote_problem)
    if local_epoch == remote_epoch:
        return None
    local_description = repr(local_epoch) if local_epoch is not None else "absent"
    remote_description = repr(remote_epoch) if remote_epoch is not None else "absent"
    return _epoch_refusal(
        "store epoch mismatch: "
        f"local epoch {local_description}; remote {remote_ref} epoch {remote_description}"
    )


def store_epoch_merge_target(
    tracker: str | os.PathLike[str], remote_ref: str
) -> tuple[str | None, str | None]:
    """Pin *remote_ref*, then check and return the exact commit safe to merge.

    Remote-tracking refs are updated by fetch outside the store write lock. Resolving
    first prevents a concurrent fetch from swapping a checked tree for a different
    epoch between the guard and the merge. The caller must merge the returned commit,
    never resolve ``remote_ref`` again for that operation.
    """
    tracker_path = os.fspath(tracker)
    try:
        resolved = run_git(
            tracker_path, "rev-parse", "--verify", f"{remote_ref}^{{commit}}", check=False
        )
    except OSError as err:
        return None, _epoch_refusal(f"remote ref {remote_ref} is unreadable: {err}")
    target = resolved.stdout.strip()
    if resolved.returncode != 0 or not target:
        return None, _epoch_refusal(f"remote ref {remote_ref} is unreadable")
    return target, store_epoch_problem(tracker, target)


def _analyze(tracker: str | os.PathLike[str]) -> dict[str, str] | None:
    """The single read-parse-validate core shared by the raising gate
    (:func:`check_store_compat`) and the non-raising diagnostic
    (:func:`describe_store_compat`).

    Returns ``None`` when the store is compatible OR the record is ABSENT (implicit
    legacy), else a ``{"kind": ..., "detail": ...}`` problem dict whose ``detail``
    names the offending version/capability/parse-error together with the record path.
    A corrupt/unreadable record is reported as a problem — never as ``None`` — so the
    gate fails CLOSED rather than silently bypassing it.
    """
    path = _record_path(tracker)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None  # ABSENT → implicit legacy (format version 0), compatible.
    except OSError as err:
        # A truncation-adjacent read error, permission denied, etc. — a PRESENT record
        # we cannot read is corrupt-for-our-purposes, so fail closed (do not treat as
        # absent).
        return {
            "kind": "corrupt_record",
            "detail": f"store-compat record at {path} is unreadable: {err}",
        }

    try:
        record = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as err:
        return {
            "kind": "corrupt_record",
            "detail": f"store-compat record at {path} is not valid JSON: {err}",
        }

    if not isinstance(record, dict):
        return {
            "kind": "corrupt_record",
            "detail": (
                f"store-compat record at {path} is malformed: expected a JSON object, "
                f"got {type(record).__name__}"
            ),
        }

    version = record.get("format_version")
    # bool is an int subclass — reject it so `true`/`false` is not read as 1/0.
    if not isinstance(version, int) or isinstance(version, bool):
        return {
            "kind": "corrupt_record",
            "detail": (
                f"store-compat record at {path} is malformed: 'format_version' must be an integer"
            ),
        }

    caps = record.get("required_capabilities")
    if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
        return {
            "kind": "corrupt_record",
            "detail": (
                f"store-compat record at {path} is malformed: 'required_capabilities' "
                "must be a list of strings"
            ),
        }

    if version not in KNOWN_FORMAT_VERSIONS:
        return {
            "kind": "unknown_format_version",
            "detail": (
                f"store-compat record at {path} declares format_version {version}, which "
                f"this rebar cannot interpret (known versions: {sorted(KNOWN_FORMAT_VERSIONS)}) "
                "— the store was written by a newer rebar; upgrade to mutate it"
            ),
        }

    for cap in caps:
        if cap not in KNOWN_CAPABILITIES:
            return {
                "kind": "unknown_capability",
                "detail": (
                    f"store-compat record at {path} requires capability {cap!r}, which this "
                    "rebar does not provide — the store was written by a newer rebar; "
                    "upgrade to mutate it"
                ),
            }

    return None


def check_store_compat(tracker: str | os.PathLike[str]) -> None:
    """Fail CLOSED (raise :class:`StoreIncompatibleError`) when the store's committed
    ``.store-compat.json`` cannot be interpreted; return ``None`` when it is
    compatible or ABSENT (implicit legacy pass-through).

    Called inside :func:`rebar._store.lock.acquire` — the single write-lock chokepoint
    — so every locked mutation is gated once, and by the explicit gates on the
    lock-less publishing paths (``fsck_recover``, the reconciler's outbound apply).
    """
    problem = _analyze(tracker)
    if problem is not None:
        raise StoreIncompatibleError(problem["detail"])


def write_compat_record(tracker: str | os.PathLike[str]) -> None:
    """Write the current-version ``.store-compat.json`` record atomically (the ensure
    unit stamps it at init). Uses :func:`rebar._store.fsutil.atomic_write` so a reader
    never observes a torn record."""
    epoch: str | None = None
    try:
        with open(_record_path(tracker), encoding="utf-8") as fh:
            existing = json.loads(fh.read())
        if isinstance(existing, dict) and isinstance(existing.get("epoch"), str):
            epoch = existing["epoch"]
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    record: dict[str, object] = {
        "format_version": CURRENT_FORMAT_VERSION,
        "required_capabilities": [],
    }
    if epoch is not None:
        record["epoch"] = epoch
    body = json.dumps(record, indent=2, sort_keys=True) + "\n"
    fsutil.atomic_write(_record_path(tracker), body)


def describe_store_compat(tracker: str | os.PathLike[str]) -> dict[str, str] | None:
    """NON-raising diagnostic twin of :func:`check_store_compat`: return a
    ``{"kind": ..., "detail": ...}`` dict describing the incompatibility/corruption
    (``kind`` ∈ ``{"unknown_format_version", "unknown_capability", "corrupt_record"}``),
    or ``None`` when the store is compatible or the record is ABSENT.

    ``fsck``'s read-only diagnostic surfaces this as a structured ``compat_error``
    object (and a WARNING line) WITHOUT blocking reads — so an operator can inspect an
    incompatible store even though every write is gated closed."""
    return _analyze(tracker)
