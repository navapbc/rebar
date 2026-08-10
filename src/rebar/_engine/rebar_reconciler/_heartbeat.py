"""Lease-heartbeat worker for one reconciler process lock.

The orchestrator owns lock acquisition and release; this module owns only the
daemon renewal loop and its observable lost-lease diagnostics. Keeping that
single call-graph seam separate leaves ``__main__`` focused on route and process
orchestration while retaining ``_Heartbeat`` as its imported compatibility name.

The worker intentionally has no acquisition or release methods. Its caller
threads the latest observed OID through ``current_oid()``, then owns the final
release in a ``finally`` block. That ownership split prevents this daemon from
turning transient renewal failures into an independent lock lifecycle.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path


def _describe_authoritative_holder(advisory, repo_root: Path) -> str:
    """Best-effort description of the lock ref after a renewal loses its CAS.

    The diagnostic distinguishes a real takeover from an absent ref and from a
    transport/read failure. It deliberately catches every provider exception:
    observing the winner is useful for post-mortems, but must never mask or
    delay the pass-abort signal that protects mutation safety.
    """
    try:
        ref_lock = advisory._load_ref_lock()
        state = ref_lock.read(
            repo_root,
            ref_lock.LOCK_REF,
            remote=advisory._lock_remote(repo_root),
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic cannot mask abort
        return f"ref state UNREADABLE ({exc!r})"
    if state is None:
        return "ref now ABSENT"
    return f"ref now oid={state.oid} holder={state.holder!r} fence={state.fence}"


def _emit_lost_lease(pass_id: str, held_oid: str, holder_detail: str) -> None:
    """Render the stable operator diagnostic for a pass that must abort."""
    print(
        f"ERROR: reconcile heartbeat lost the lease "
        f"(pass_id={pass_id!r}, we held {held_oid}; {holder_detail})"
        f" — aborting pass",
        file=sys.stderr,
    )


class Heartbeat:
    """Renew a pass lease and signal when ownership is lost or stolen."""

    def __init__(self, advisory_mod, pass_id: str, repo_root: Path, oid: str, interval: int):
        self._advisory = advisory_mod
        self._pass_id = pass_id
        self._repo_root = repo_root
        self._oid = oid
        self._interval = interval
        self.lock_lost = threading.Event()
        self._stop = threading.Event()
        self._oid_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._lease_lost_cls = advisory_mod._load_ref_lock().LeaseLostError

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="reconciler-heartbeat", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                new_oid = self._advisory.renew_pass_lock(
                    self._pass_id, self._repo_root, self.current_oid()
                )
                with self._oid_lock:
                    self._oid = new_oid
            except self._lease_lost_cls:
                # Probe the authoritative ref for a useful takeover diagnostic;
                # failure remains in-band and never delays the abort signal.
                held = _describe_authoritative_holder(self._advisory, self._repo_root)
                _emit_lost_lease(self._pass_id, self.current_oid(), held)
                self.lock_lost.set()
                return
            except Exception as exc:  # noqa: BLE001 - transient renewal failure
                print(
                    f"WARN: reconcile heartbeat renew failed (retrying): {exc!r}",
                    file=sys.stderr,
                )

    def current_oid(self) -> str:
        with self._oid_lock:
            return self._oid

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)
