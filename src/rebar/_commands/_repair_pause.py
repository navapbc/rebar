"""Durable reconciler-pause ownership for destructive repair commands.

Repairs coordinate through the same provider-neutral ``refs/reconciler/gate``
CAS ref as bridge pause/resume.  This scope owns one unique reason token, proves
the leased pass lock free only after the pause is durable, and clears only the
exact pause snapshot it created.  Every uncertainty fails closed so an operator
can inspect and recover the durable marker instead of racing a reconciler.
"""

from __future__ import annotations

import datetime
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

from rebar import config
from rebar._commands import identity
from rebar._commands._seam import CommandError

InFlightProbe = Callable[[Path], bool]


class RepairPauseError(CommandError):
    """A fail-closed repair-safety refusal at the command boundary."""

    def __init__(self, message: str, *, legacy_report_line: str | None = None) -> None:
        super().__init__(message)
        self.legacy_report_line = legacy_report_line


def _backend(repo_root: Path) -> tuple[ModuleType, str | None]:
    """Load the bundled ref backend and resolve its authoritative remote."""
    from rebar._engine import engine_dir

    engine = str(engine_dir())
    if engine not in sys.path:
        sys.path.insert(0, engine)
    from rebar_reconciler import _advisory_lock as advisory

    return advisory._load_ref_lock(), advisory._lock_remote(repo_root)


def _safety_error(surface: str, detail: str) -> RepairPauseError:
    return RepairPauseError(f"Error: {surface} repair {detail}")


def _clear_owned_pause(
    surface: str,
    repo_root: Path,
    ref_lock: ModuleType,
    remote: str | None,
    token: str,
    created_oid: str,
) -> None:
    """Delete only the unchanged pause created by this repair invocation."""
    try:
        snapshot = ref_lock.read_pause_with_oid(repo_root, remote=remote)
    except Exception as exc:
        raise _safety_error(
            surface,
            "could not verify its reconciliation pause during cleanup; "
            "the durable pause was left for operator recovery",
        ) from exc
    if snapshot is None:
        raise _safety_error(
            surface,
            "lost its reconciliation pause during cleanup; operator recovery is required",
        )

    pause, observed_oid = snapshot
    if pause.get("reason") != token or observed_oid != created_oid:
        raise _safety_error(
            surface,
            "found its reconciliation pause replaced during cleanup; "
            "the current pause was not cleared",
        )
    try:
        deleted = ref_lock.release(
            repo_root,
            ref_lock.GATE_REF,
            oid=observed_oid,
            remote=remote,
        )
    except Exception as exc:
        raise _safety_error(
            surface,
            "could not clear its reconciliation pause; "
            "the durable pause was left for operator recovery",
        ) from exc
    if not deleted:
        raise _safety_error(
            surface,
            "lost the cleanup CAS for its reconciliation pause; the current pause was not cleared",
        )


@contextmanager
def owned_repair_pause(
    surface: str,
    repo_root=None,
    *,
    in_flight_probe: InFlightProbe,
) -> Iterator[None]:
    """Hold a uniquely owned durable pause around one destructive repair.

    Identity and the pre-existing gate are checked before the repair mutates.
    Once pause creation succeeds, every exit path re-reads one document/OID
    snapshot and performs exactly one observed-OID delete attempt.
    """
    root = Path(config.repo_root(repo_root))
    who = identity._git_email(root)
    if who is None:
        raise _safety_error(surface, "requires a configured git user.email")

    try:
        ref_lock, remote = _backend(root)
        existing = ref_lock.read_pause_with_oid(root, remote=remote)
    except Exception as exc:
        raise _safety_error(surface, "cannot safely read the reconciliation pause") from exc
    if existing is not None:
        reason = existing[0]["reason"]
        raise _safety_error(
            surface,
            f"refuses to replace the existing reconciliation pause ({reason!r})",
        )

    token = f"repair:{surface}:{uuid.uuid4()}"
    paused_at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    try:
        created_oid = ref_lock.set_pause(
            root,
            reason=token,
            who=who,
            paused_at=paused_at.isoformat().replace("+00:00", "Z"),
            remote=remote,
        )
    except Exception as exc:
        raise _safety_error(surface, "could not acquire its reconciliation pause") from exc

    try:
        try:
            in_flight = in_flight_probe(root)
        except Exception as exc:
            raise _safety_error(
                surface,
                "cannot prove refs/reconciler/lock is free; refusing to repair",
            ) from exc
        if in_flight:
            raise RepairPauseError(
                "Error: a reconciler pass is in flight (refs/reconciler/lock held or "
                "unreadable) — refusing to repair; retry once the pass completes",
                legacy_report_line=(
                    "ABORT: a reconciler pass is in flight "
                    "(refs/reconciler/lock held or unreadable) — refusing to repair; "
                    "retry once the pass completes"
                ),
            )
        yield
    finally:
        _clear_owned_pause(surface, root, ref_lock, remote, token, created_oid)
