"""Durable last-pass publication and bridge status snapshots.

``refs/reconciler/last-pass`` is the small authoritative witness.  A matching
``<store>/.bridge_state/last-pass.json`` may add local detail, but is
never allowed to replace or contradict the ref record.  Status reads the pause,
live lease, and completion witnesses once and applies one shared verdict order.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import random
import sys
import tempfile
import traceback
from collections.abc import Callable, Mapping
from pathlib import Path
from time import sleep as _sleep
from typing import Any

SCHEMA_VERSION = 2
# Older witnesses (schema 1, before the additive disposition block) still validate and
# read — the additive fields default to null rather than failing an older reader.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})
_OUTCOME_TALLY_BUCKETS: tuple[str, ...] = (
    "applied",
    "recovered",
    "deferred",
    "failed",
    "skipped",
)
LAST_PASS_REF = "refs/reconciler/last-pass"
# Relative to the RESOLVED store root, NOT the repo root: the store is RELOCATABLE
# (``REBAR_TRACKER_DIR`` / ``tracker.dir``), so join it onto ``_store_dir(repo_root)``.
DETAIL_IN_STORE = Path(".bridge_state/last-pass.json")
HEALTHY_VERDICTS = frozenset({"HEALTHY", "PAUSED", "RUNNING"})
_ZERO_OID = "0" * 40
# Bug paediatric-orchestral-anemone: ADDITIVE jitter on top of the CAS-publish base schedule
# so two passes that collide once on LAST_PASS_REF do not sleep identical durations, wake in
# lockstep, and re-collide (thundering herd). Mirrors the additive-jitter idiom in
# `push_classify._cas_backoff` / `_CAS_BACKOFF_JITTER` and the Jira DC transport. Kept small so
# consecutive base values stay ordered — `[base, 1.25*base]` never reaches the next base.
_CAS_BACKOFF_JITTER = 0.25


class LastPassError(RuntimeError):
    """A last-pass witness could not be resolved, decoded, or published."""


def _load_ref_lock():
    key = "rebar_reconciler._ref_lock"
    if key in sys.modules:
        return sys.modules[key]
    path = Path(__file__).with_name("_ref_lock.py")
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise LastPassError(f"cannot load ref-lock implementation at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def _store_dir(repo_root: Path) -> Path:
    """The RESOLVED ticket store for ``repo_root`` — ``REBAR_TRACKER_DIR`` >
    ``tracker.dir`` > the default name under the root (``rebar.config.tracker_dir``).
    Every local witness path below is joined onto THIS, never onto a hardcoded name."""
    from rebar.config import tracker_dir

    return tracker_dir(repo_root)


def _remote(repo_root: Path) -> str | None:
    """Return the configured authoritative remote when it exists in this clone."""
    from rebar.config import ConfigError, compose_config

    try:
        remote = compose_config().sync.remote or "origin"
    except ConfigError:
        remote = "origin"
    completed = __import__("subprocess").run(
        ["git", "-C", str(repo_root), "remote", "get-url", remote],
        capture_output=True,
        check=False,
    )
    return remote if completed.returncode == 0 else None


def resolve_environment_id(repo_root: Path, explicit: str | None = None) -> str:
    """Resolve target/producer identity without guessing a reconciler fallback."""
    if explicit is not None:
        value = explicit.strip()
        if not value:
            raise LastPassError("target environment id must not be empty")
        return value
    from rebar.config import environment_id_or_none

    configured = environment_id_or_none()
    if configured is not None:
        value = configured.strip()
        if not value:
            raise LastPassError("REBAR_ENV_ID is set but empty")
        return value
    path = _store_dir(repo_root) / ".env-id"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise LastPassError(f"cannot read local environment id from {path}: {exc}") from exc
    if not value:
        raise LastPassError(f"local environment id file is empty: {path}")
    return f"local:{value}"


def _utc_now() -> str:
    value = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    return value.replace("+00:00", "Z")


def _parse_completed_at(value: Any) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise LastPassError("last-pass completed_at must be a UTC timestamp ending in Z")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise LastPassError(f"invalid last-pass completed_at: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise LastPassError("last-pass completed_at must be UTC")
    return parsed


def _normalized_outcome_tally(raw: Any) -> dict[str, int] | None:
    """Project a raw tally onto the five canonical buckets (0-filled), or None."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise LastPassError("last-pass outcome_tally must be a mapping or null")
    return {b: int(raw.get(b, 0) or 0) for b in _OUTCOME_TALLY_BUCKETS}


def _validate_record(doc: Any) -> dict[str, Any]:
    if not isinstance(doc, dict) or doc.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
        raise LastPassError("last-pass ref does not contain a supported schema version")
    for name in ("pass_id", "environment_id", "outcome", "completed_at"):
        if not isinstance(doc.get(name), str) or not doc[name]:
            raise LastPassError(f"last-pass field {name!r} must be a non-empty string")
    if doc["outcome"] not in {"success", "failure"}:
        raise LastPassError("last-pass outcome must be 'success' or 'failure'")
    failure_kind = doc.get("failure_kind")
    if doc["outcome"] == "failure" and not isinstance(failure_kind, str):
        raise LastPassError("failed last-pass record requires failure_kind")
    if failure_kind is not None and not isinstance(failure_kind, str):
        raise LastPassError("last-pass failure_kind must be a string or null")
    fence = doc.get("lock_fence")
    if fence is not None and (not isinstance(fence, int) or isinstance(fence, bool) or fence < 0):
        raise LastPassError("last-pass lock_fence must be a non-negative integer or null")
    degraded = doc.get("degraded")
    if degraded is not None and not isinstance(degraded, bool):
        raise LastPassError("last-pass degraded must be a boolean or null")
    _parse_completed_at(doc["completed_at"])
    normalized = dict(doc)
    normalized.setdefault("failure_kind", None)
    normalized.setdefault("lock_fence", None)
    normalized["outcome_tally"] = _normalized_outcome_tally(doc.get("outcome_tally"))
    normalized.setdefault("degraded", None)
    return normalized


def _read_record(
    repo_root: Path, ref_lock, remote: str | None
) -> tuple[dict[str, Any] | None, str | None]:
    oid = ref_lock._ref_oid(repo_root, LAST_PASS_REF, remote=remote)
    if oid is None:
        return None, None
    raw = ref_lock._read_blob_bytes(repo_root, LAST_PASS_REF, oid)
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LastPassError(f"last-pass ref is not valid JSON: {exc}") from exc
    return _validate_record(decoded), oid


def _read_matching_detail(
    repo_root: Path, record: dict[str, Any] | None
) -> tuple[str, dict[str, Any] | None]:
    path = _store_dir(repo_root) / DETAIL_IN_STORE
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "missing", None
    except (OSError, json.JSONDecodeError):
        return "missing", None
    if not isinstance(decoded, dict):
        return "missing", None
    if record is None:
        return "mismatched", None
    if (
        decoded.get("pass_id") != record["pass_id"]
        or decoded.get("environment_id") != record["environment_id"]
    ):
        return "mismatched", None
    return "matching", decoded


def snapshot(
    repo_root: Path,
    *,
    target_environment_id: str | None = None,
    max_age_seconds: int | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Read one status snapshot and classify it with the canonical precedence."""
    target = resolve_environment_id(repo_root, target_environment_id)
    ref_lock = _load_ref_lock()
    remote = _remote(repo_root)
    record, record_oid = _read_record(repo_root, ref_lock, remote)
    pause = ref_lock.read_pause(repo_root, remote=remote)
    lock = ref_lock.read(repo_root, ref_lock.LOCK_REF, remote=remote)
    detail_status, detail = _read_matching_detail(repo_root, record)

    if pause is not None:
        verdict = "PAUSED"
    elif lock is not None:
        verdict = "RUNNING"
    elif record is None:
        verdict = "NEVER_RUN"
    elif record["environment_id"] != target:
        verdict = "FOREIGN"
    elif record["outcome"] == "failure":
        verdict = "FAILED"
    elif (
        max_age_seconds is not None
        and (
            (now or dt.datetime.now(dt.timezone.utc)) - _parse_completed_at(record["completed_at"])
        ).total_seconds()
        > max_age_seconds
    ):
        verdict = "STALE"
    else:
        verdict = "HEALTHY"

    result: dict[str, Any] = {
        "verdict": verdict,
        "target_environment_id": target,
        "record_oid": record_oid,
        "detail_status": detail_status,
        "pause": pause,
        "lock": None,
    }
    if record is not None:
        result.update(record)
    else:
        result.update(
            pass_id=None,
            environment_id=None,
            outcome=None,
            failure_kind=None,
            completed_at=None,
            lock_fence=None,
        )
    if detail is not None:
        protected = set(result)
        result.update({k: v for k, v in detail.items() if k not in protected})
        result["detail"] = detail
    if lock is not None:
        live = {
            "oid": lock.oid,
            "holder": lock.holder,
            "lease_secs": lock.lease_secs,
            "heartbeat_ns": lock.heartbeat_ns,
            "fence": lock.fence,
        }
        result["lock"] = live
        result.update(
            lock_oid=lock.oid,
            lock_holder=lock.holder,
            lock_lease_secs=lock.lease_secs,
            live_lock_fence=lock.fence,
        )
    return result


def _write_detail(repo_root: Path, record: dict[str, Any], detail: dict[str, Any] | None) -> None:
    path = _store_dir(repo_root) / DETAIL_IN_STORE
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {**(detail or {}), **record}
    fd, temporary = tempfile.mkstemp(prefix="last-pass-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, sort_keys=True, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def publish(
    repo_root: Path,
    *,
    pass_id: str,
    environment_id: str | None = None,
    outcome: str,
    failure_kind: str | None = None,
    lock_fence: int | None = None,
    outcome_tally: Mapping[str, Any] | None = None,
    degraded: bool | None = None,
    detail: dict[str, Any] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """CAS-publish the durable witness, then replace the single local detail file."""
    if outcome not in {"success", "failure"}:
        raise LastPassError("outcome must be 'success' or 'failure'")
    if outcome == "failure" and not failure_kind:
        raise LastPassError("failure_kind is required for a failed pass")
    record = {
        "schema_version": SCHEMA_VERSION,
        "pass_id": pass_id,
        "environment_id": resolve_environment_id(repo_root, environment_id),
        "outcome": outcome,
        "failure_kind": failure_kind,
        "completed_at": _utc_now(),
        "lock_fence": lock_fence,
        "outcome_tally": _normalized_outcome_tally(outcome_tally),
        "degraded": None if degraded is None else bool(degraded),
    }
    ref_lock = _load_ref_lock()
    remote = _remote(repo_root)
    raw = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    new_oid = ref_lock._hash_object(repo_root, raw)
    old_oid = ref_lock._ref_oid(repo_root, LAST_PASS_REF, remote=remote) or _ZERO_OID
    sleeper = _sleep if sleep_fn is None else sleep_fn
    for attempt in range(3):
        if ref_lock._cas_advance(
            repo_root,
            LAST_PASS_REF,
            new_oid=new_oid,
            old_oid=old_oid,
            remote=remote,
        ):
            _write_detail(repo_root, record, detail)
            return record
        if attempt == 2:
            break
        base = (0.1, 0.2)[attempt]
        sleeper(base * (1.0 + random.random() * _CAS_BACKOFF_JITTER))
        old_oid = ref_lock._ref_oid(repo_root, LAST_PASS_REF, remote=remote) or _ZERO_OID
    raise LastPassError("last-pass ref changed during all three CAS publication attempts")


def current_lock_fence(repo_root: Path, pass_id: str) -> int | None:
    """Capture live fence provenance while the process still owns its lease."""
    ref_lock = _load_ref_lock()
    state = ref_lock.read(repo_root, ref_lock.LOCK_REF, remote=_remote(repo_root))
    return state.fence if state is not None and state.holder == pass_id else None


def publish_process_result(
    repo_root: Path,
    pass_id: str,
    exit_code: int,
    *,
    failure_kind: str | None = None,
    outcome_tally: Mapping[str, Any] | None = None,
    degraded: bool | None = None,
) -> dict[str, Any]:
    """Publish a process result while its heartbeat and pass lock remain live."""
    outcome = "success" if exit_code == 0 else "failure"
    semantic_failure = failure_kind
    if outcome == "failure" and semantic_failure is None:
        semantic_failure = "reschedule" if exit_code == 75 else "operational_failure"
    from rebar.config import environment_id_or_none

    return publish(
        repo_root,
        pass_id=pass_id,
        environment_id=environment_id_or_none(),
        outcome=outcome,
        failure_kind=None if outcome == "success" else semantic_failure,
        lock_fence=current_lock_fence(repo_root, pass_id),
        outcome_tally=outcome_tally,
        degraded=degraded,
        detail={"process_exit_code": exit_code},
    )


def finalize_process(repo_root: Path, pass_id: str, run: Callable[[], int]) -> int:
    """Run and publish one mutating process before its caller releases the lease."""
    failure_kind = None
    try:
        exit_code = run()
    except Exception as exc:  # noqa: BLE001 — process boundary records the hard failure
        print(f"ERROR: run_pass raised: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        exit_code = 1
        failure_kind = "exception"
    try:
        publish_process_result(repo_root, pass_id, exit_code, failure_kind=failure_kind)
    except Exception as exc:  # noqa: BLE001 — publication failure forces nonzero
        print(f"ERROR: cannot publish reconciler last-pass witness: {exc}", file=sys.stderr)
        return 1
    return exit_code


def release_process_lock(
    advisory, heartbeat, acquired: bool, pass_id: str, repo_root: Path
) -> None:
    """Stop heartbeat and release its latest observed lease without masking the result."""
    if heartbeat is not None:
        heartbeat.stop()
    if not acquired:
        return
    try:
        if heartbeat is not None:
            advisory.release_pass_lock(pass_id, repo_root, oid=heartbeat.current_oid())
        else:
            advisory.release_pass_lock(pass_id, repo_root)
    except Exception as exc:  # noqa: BLE001 — final cleanup never masks the pass result
        print(f"WARN: release_pass_lock failed for pass_id={pass_id!r}: {exc!r}", file=sys.stderr)
