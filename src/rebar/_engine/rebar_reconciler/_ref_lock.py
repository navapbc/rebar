"""Bare-ref CAS lock primitive for the reconciler pass-lock / phase-gate.

A lock lives as a git **ref -> blob** (``refs/reconciler/lock`` /
``refs/reconciler/gate``), NOT as a file in the tickets working tree — so it is
never union-merged and never taxes the ticket-event hot paths. The blob is a
newline-terminated UTF-8 JSON document::

    {"holder": "<pass id>", "lease_secs": 120, "heartbeat_ns": 173…, "fence": 0}

* ``heartbeat_ns`` (``time.time_ns()``) is the acquisition / renew anchor — it is
  diagnostic only; the skew-proof lease-expiry rule (C2) reads ``fence`` + the ref
  oid, not this wall clock.
* ``fence`` is a monotonic **progress-witness / generation counter** seeded to 0
  on acquire; C2 increments it on renew/steal. It is NOT a full fencing token
  (no stale-writer rejection is claimed of the protected resource).

**CAS contract.** ``git hash-object -w --stdin`` plants the blob and yields its
OID; ``git update-ref <ref> <oid> <old>`` then advances the ref only if it still
points at ``<old>``:

* **acquire** is create-only — ``<old>`` is 40 zeros, so the CAS fails iff the ref
  already exists. That exit-128 means "lock already held" — definitive, NOT
  retried; :func:`acquire` raises :class:`RefLockHeldError`.
* **release** deletes the ref (``git update-ref -d <ref> <old-oid>``) against the
  exact observed OID. A CAS mismatch (ref already gone, or now owned by someone
  else) is a benign **idempotent success**, not an error.

Only genuinely transient CAS races are retried (via the shared
``_cas_advance_with_retry`` in :mod:`_advisory_lock`); acquire/release classify
their definitive exit-128 outcomes through the shared :func:`_cas_once` seam so
there is exactly ONE CAS discriminator in the reconciler.

**Distributed operation.** With ``remote is None`` the CAS is a pure local
``update-ref`` (unit tests / single-clone). With a ``remote`` the authoritative
ref lives on that remote: :func:`read` force-fetches ``<ref>:<ref>`` first, and
acquire/release do the CAS as a ``git push --force-with-lease=<ref>:<old>``
(explicit lease-ref form, never bare) — the remote-side equivalent of the
old-oid ``update-ref`` CAS. The reconciler passes ``remote="origin"`` (wired in
C3); AC0 proves the ``refs/reconciler/*`` refspec round-trips through GitHub.

**Fail-closed reads.** :func:`read` raises :class:`RefLockCorruptError` on a
corrupt / partial / empty blob and :class:`RefLockTimeoutError` on a subprocess
timeout; callers treat either (indeed any read failure) as HELD, never free.

No lease-expiry / steal / ``renew()`` logic lives here — that is C2, which
extends this module.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reuse the shared CAS discriminator + single-shot seam from _advisory_lock.
#
# There is deliberately ONE CAS classifier in the reconciler. We load the
# sibling via the package's by-path loader idiom (the reconciler package is
# routinely exec'd standalone / shadowed by a test package), matching how
# _advisory_lock itself loads _concurrency.
# ---------------------------------------------------------------------------

try:
    from rebar_reconciler._loader import lazy_load
except ImportError:  # standalone load without package context
    _loader_key = "rebar_reconciler._loader"
    if _loader_key not in sys.modules:
        _loader_spec = importlib.util.spec_from_file_location(
            _loader_key, Path(__file__).parent / "_loader.py"
        )
        assert _loader_spec is not None and _loader_spec.loader is not None
        _loader_mod = importlib.util.module_from_spec(_loader_spec)
        sys.modules[_loader_key] = _loader_mod
        _loader_spec.loader.exec_module(_loader_mod)  # type: ignore[union-attr]
    lazy_load = sys.modules[_loader_key].lazy_load

_advisory = lazy_load("rebar_reconciler_ref_lock_advisory", "_advisory_lock.py")
_cas_once = _advisory._cas_once
_is_cas_mismatch = _advisory._is_cas_mismatch

# ---------------------------------------------------------------------------
# Ref names + timeouts (see module docstring / ADR).
# ---------------------------------------------------------------------------

LOCK_REF = "refs/reconciler/lock"
GATE_REF = "refs/reconciler/gate"

_ZERO_OID = "0" * 40  # create-only CAS old-value: "ref must not exist"

_LOCAL_TIMEOUT_SECS = 5.0
_REMOTE_TIMEOUT_SECS = 30.0

# Blob field contract (Design decision 3): the closed set + their runtime types.
_REQUIRED_FIELDS = ("holder", "lease_secs", "heartbeat_ns", "fence")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RefLockError(RuntimeError):
    """Base class for ref-lock failures."""


class RefLockHeldError(RefLockError):
    """Raised by :func:`acquire` when the ref already exists (lock already held)."""


class RefLockCorruptError(ValueError):
    """Raised by :func:`read` when the lock blob cannot be parsed.

    Triggered by any of: empty / non-UTF-8 bytes, invalid JSON, a missing
    required field, ``fence`` not a non-negative int, or ``lease_secs`` not a
    positive number. Carries the raw bytes for debugging. Callers treat a
    corrupt blob as HELD (fail-closed) — never free.
    """

    def __init__(self, message: str, *, raw: bytes = b"") -> None:
        super().__init__(message)
        self.raw = raw


class RefLockTimeoutError(RefLockError):
    """Raised when a git subprocess exceeds its timeout (fail-closed: treat as HELD)."""


# ---------------------------------------------------------------------------
# Lock state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RefLockState:
    """A decoded lock blob plus the ref OID it was read from (the CAS anchor)."""

    holder: str
    lease_secs: float
    heartbeat_ns: int
    fence: int
    oid: str


# ---------------------------------------------------------------------------
# Blob (de)serialization
# ---------------------------------------------------------------------------


def _encode_blob(holder: str, lease_secs: float, heartbeat_ns: int, fence: int) -> bytes:
    """Serialize the lock blob as newline-terminated UTF-8 JSON (stable key order)."""
    doc = {
        "holder": holder,
        "lease_secs": lease_secs,
        "heartbeat_ns": heartbeat_ns,
        "fence": fence,
    }
    return (json.dumps(doc, sort_keys=True) + "\n").encode("utf-8")


def _decode_blob(raw: bytes, oid: str) -> RefLockState:
    """Decode a lock blob, failing CLOSED on anything malformed.

    Raises :class:`RefLockCorruptError` on empty / non-UTF-8 / invalid-JSON /
    missing-field / wrong-type content so callers treat it as HELD.
    """
    if not raw:
        raise RefLockCorruptError("empty lock blob", raw=raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RefLockCorruptError(f"non-UTF-8 lock blob: {exc}", raw=raw) from exc
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RefLockCorruptError(f"invalid JSON in lock blob: {exc}", raw=raw) from exc
    if not isinstance(doc, dict):
        raise RefLockCorruptError(f"lock blob is not a JSON object: {type(doc).__name__}", raw=raw)
    for field_name in _REQUIRED_FIELDS:
        if field_name not in doc:
            raise RefLockCorruptError(f"lock blob missing field {field_name!r}", raw=raw)
    fence = doc["fence"]
    # bool is an int subclass — reject it explicitly (a boolean fence is corrupt).
    if isinstance(fence, bool) or not isinstance(fence, int) or fence < 0:
        raise RefLockCorruptError(f"fence must be a non-negative int, got {fence!r}", raw=raw)
    lease_secs = doc["lease_secs"]
    if isinstance(lease_secs, bool) or not isinstance(lease_secs, (int, float)) or lease_secs <= 0:
        raise RefLockCorruptError(
            f"lease_secs must be a positive number, got {lease_secs!r}", raw=raw
        )
    heartbeat_ns = doc["heartbeat_ns"]
    if isinstance(heartbeat_ns, bool) or not isinstance(heartbeat_ns, int):
        raise RefLockCorruptError(f"heartbeat_ns must be an int, got {heartbeat_ns!r}", raw=raw)
    return RefLockState(
        holder=str(doc["holder"]),
        lease_secs=float(lease_secs),
        heartbeat_ns=int(heartbeat_ns),
        fence=int(fence),
        oid=oid,
    )


# ---------------------------------------------------------------------------
# Git subprocess helpers (all time-bounded; timeout -> RefLockTimeoutError)
# ---------------------------------------------------------------------------


def _git(
    repo_root: Path,
    args: list[str],
    *,
    timeout: float,
    check: bool = True,
    text: bool = True,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess:
    """Run ``git -C <repo_root> <args>`` with a timeout (the single git seam here).

    A :class:`subprocess.TimeoutExpired` is logged and re-raised as
    :class:`RefLockTimeoutError` (fail-closed — callers treat it as HELD). With
    ``check=True`` a non-zero exit raises :class:`subprocess.CalledProcessError`
    (so the shared :func:`_cas_once` seam can classify a CAS mismatch). ``text``
    toggles str vs raw-bytes capture (blob content is read as bytes); ``stdin``
    feeds ``git hash-object`` its payload.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            input=stdin,
            capture_output=True,
            text=text,
            check=check,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning(
            "ref-lock: git %s timed out after %ss (fail-closed: treat as HELD)",
            " ".join(args[:2]),
            timeout,
        )
        raise RefLockTimeoutError(
            f"git {' '.join(args[:2])} timed out after {timeout}s (fail-closed: treat as HELD)"
        ) from exc


def _git_bytes(repo_root: Path, args: list[str], *, timeout: float) -> bytes:
    """Run git capturing raw stdout bytes (for blob content)."""
    return _git(repo_root, args, timeout=timeout, text=False).stdout


def _hash_object(repo_root: Path, blob: bytes) -> str:
    """Plant *blob* in the object store via ``git hash-object -w --stdin``; return its OID."""
    result = _git(
        repo_root,
        ["hash-object", "-w", "--stdin"],
        timeout=_LOCAL_TIMEOUT_SECS,
        text=False,
        stdin=blob,
    )
    return result.stdout.decode("utf-8").strip()


def _fetch_ref(repo_root: Path, ref: str, remote: str) -> None:
    """Force-sync the local *ref* from *remote* (best-effort — absent remote ref is fine)."""
    # ``+<ref>:<ref>`` force-updates the local ref to the remote truth; an absent
    # remote ref makes fetch exit non-zero, which we swallow (ref stays / becomes
    # absent locally = free).
    _git(repo_root, ["fetch", remote, f"+{ref}:{ref}"], timeout=_REMOTE_TIMEOUT_SECS, check=False)


# ---------------------------------------------------------------------------
# Ref read
# ---------------------------------------------------------------------------


def read(repo_root: Path, ref: str, *, remote: str | None = None) -> RefLockState | None:
    """Return the current :class:`RefLockState`, or ``None`` if the lock is free.

    With a *remote* the ref is force-synced from it first (the remote is
    authoritative). ``None`` means the ref is absent (free); a present-but-corrupt
    blob raises :class:`RefLockCorruptError` (callers treat that as HELD).
    """
    if remote is not None:
        _fetch_ref(repo_root, ref, remote)

    oid_result = _git(
        repo_root,
        ["rev-parse", "--verify", "--quiet", ref],
        timeout=_LOCAL_TIMEOUT_SECS,
        check=False,
    )
    if oid_result.returncode != 0:
        return None  # ref absent -> free
    oid = oid_result.stdout.strip()
    if not oid:
        return None

    obj_type = _git(repo_root, ["cat-file", "-t", oid], timeout=_LOCAL_TIMEOUT_SECS).stdout.strip()
    if obj_type == "blob":
        raw = _git_bytes(repo_root, ["cat-file", "blob", oid], timeout=_LOCAL_TIMEOUT_SECS)
    elif obj_type == "commit":
        # AC0 ref->tiny-commit fallback: the blob lives at <ref>:lock.json.
        raw = _git_bytes(
            repo_root, ["cat-file", "blob", f"{ref}:lock.json"], timeout=_LOCAL_TIMEOUT_SECS
        )
    else:
        raise RefLockCorruptError(f"ref {ref} points at unexpected object type {obj_type!r}")
    return _decode_blob(raw, oid)


# ---------------------------------------------------------------------------
# Acquire / release
# ---------------------------------------------------------------------------


def acquire(
    repo_root: Path,
    ref: str,
    *,
    holder: str,
    lease_secs: float,
    remote: str | None = None,
) -> str:
    """Create-only CAS acquire of *ref*; return the new blob OID.

    Plants ``{holder, lease_secs, heartbeat_ns, fence=0}`` as a blob, then does a
    create-only CAS (old = 40 zeros). A CAS mismatch means the ref already exists
    (lock held) — definitive, NOT retried: raises :class:`RefLockHeldError`.
    """
    blob = _encode_blob(holder, lease_secs, time.time_ns(), 0)
    oid = _hash_object(repo_root, blob)

    def _plant() -> None:
        if remote is None:
            _git(repo_root, ["update-ref", ref, oid, _ZERO_OID], timeout=_LOCAL_TIMEOUT_SECS)
        else:
            _push_cas(repo_root, ref, oid, _ZERO_OID, remote)

    if not _cas_once(_plant, ref):
        logger.info(
            "ref-lock acquire failed: %s already held (create-only CAS rejected) holder=%r",
            ref,
            holder,
        )
        raise RefLockHeldError(f"lock {ref} already held")
    return oid


def release(repo_root: Path, ref: str, *, oid: str, remote: str | None = None) -> bool:
    """Observed-oid CAS release (delete) of *ref*. Idempotent.

    Deletes the ref only if it still points at *oid*. A CAS mismatch (ref already
    gone, or now owned by another holder after a steal) is a benign idempotent
    success — returns ``False`` (nothing deleted); a successful delete returns
    ``True``. Never raises on a stale oid.
    """

    def _delete() -> None:
        if remote is None:
            _git(repo_root, ["update-ref", "-d", ref, oid], timeout=_LOCAL_TIMEOUT_SECS)
        else:
            _push_delete_cas(repo_root, ref, oid, remote)

    if _cas_once(_delete, ref):
        return True
    logger.info(
        "ref-lock release: %s no longer points at %s (already released or stolen) — "
        "idempotent no-op",
        ref,
        oid,
    )
    return False


# ---------------------------------------------------------------------------
# Remote (push/fetch) CAS — the authoritative-on-remote path (wired by C3).
#
# A rejected ``--force-with-lease`` push is the remote-side equivalent of an
# update-ref CAS mismatch. git push does NOT exit 128 for that (it exits 1 with a
# "stale info" / "rejected" stderr), so we translate a rejected push into the same
# exit-128 update-ref CalledProcessError shape the shared _is_cas_mismatch/_cas_once
# seam understands — keeping exactly one CAS discriminator.
# ---------------------------------------------------------------------------

# A rejected ``--force-with-lease`` prints "stale info" (the lease mismatch) or a
# "! [rejected]" / "cannot lock ref" line — distinct from a genuine transport
# failure ("Authentication failed", "Could not resolve host"), which we do NOT
# classify as a CAS mismatch (fail-closed).
_PUSH_REJECT_MARKERS = ("stale info", "rejected", "cannot lock ref")


def _push_lease_cas(repo_root: Path, ref: str, old_oid: str, remote: str, refspec: str) -> None:
    """Do a ``--force-with-lease=<ref>:<old>`` push of *refspec* to *remote*.

    Shared by acquire (``<new-oid>:<ref>``) and release (``:<ref>`` delete). A
    rejected lease (remote ref moved) is re-raised in the update-ref exit-128
    shape so the shared :func:`_cas_once` seam classifies it as a CAS mismatch; a
    genuine transport failure is logged and re-raised (fail-closed).
    """
    result = _git(
        repo_root,
        ["push", f"--force-with-lease={ref}:{old_oid}", remote, refspec],
        timeout=_REMOTE_TIMEOUT_SECS,
        check=False,
    )
    if result.returncode == 0:
        return
    stderr = (result.stderr or "").lower()
    if any(marker in stderr for marker in _PUSH_REJECT_MARKERS):
        # Remote-side CAS mismatch — same shape the update-ref discriminator reads.
        raise subprocess.CalledProcessError(128, ["git", "update-ref", ref])
    logger.warning(
        "ref-lock: git push to %s %s failed (exit %s) — fail-closed: %s",
        remote,
        ref,
        result.returncode,
        (result.stderr or "").strip()[:200],
    )
    raise subprocess.CalledProcessError(
        result.returncode, result.args, result.stdout, result.stderr
    )


def _push_cas(repo_root: Path, ref: str, new_oid: str, old_oid: str, remote: str) -> None:
    """Create/advance CAS: push ``<new-oid>:<ref>`` under a lease on *old_oid*."""
    _push_lease_cas(repo_root, ref, old_oid, remote, f"{new_oid}:{ref}")


def _push_delete_cas(repo_root: Path, ref: str, old_oid: str, remote: str) -> None:
    """Delete CAS: push ``:<ref>`` under a lease on *old_oid* (observed-oid delete)."""
    _push_lease_cas(repo_root, ref, old_oid, remote, f":{ref}")
