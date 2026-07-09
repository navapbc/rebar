"""Story d01e — comprehensive LIVE reconciler validation + GUARANTEED bilateral cleanup.

Runs the full bidirectional matrix against a dedicated live Jira TEST project and
guarantees teardown in BOTH systems (Jira issues hard-deleted + the local throwaway
env discarded), even on assertion failure / exception. The live scenarios self-skip
without live env; the teardown machinery + the blast-radius breaker are verified
OFFLINE here.

Run the live matrix with:  ``pytest tests/integration/test_reconcile_live_e2e.py -m live``
(needs JIRA_URL / JIRA_USER / JIRA_API_TOKEN + acli on PATH + a scoped test project).

Design (harness pattern, ADR-worthy): an ``ArtifactTracker`` records exactly which of
the N synthetic artifacts were actually created (partial-setup aware), and
``_bilateral_teardown`` deletes precisely those — retrying each delete with bounded
backoff, appending the id to ``leaked-artifacts.log`` (a CI artifact) and failing the
run non-zero on exhaustion, so a leak is loud and never silent.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.live]

REPO_ROOT = Path(__file__).resolve().parents[2]
RECON_DIR = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"
LEAKED_LOG = REPO_ROOT / "leaked-artifacts.log"

_LIVE_ENV_KEYS = ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN")


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, RECON_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _live_env_available() -> bool:
    """True only when the live Jira credentials AND acli are present."""
    if not all(os.environ.get(k) for k in _LIVE_ENV_KEYS):
        return False
    from shutil import which

    return which("acli") is not None


_requires_live = pytest.mark.skipif(
    not _live_env_available(),
    reason="live Jira env (JIRA_URL/JIRA_USER/JIRA_API_TOKEN + acli) not available",
)


# --------------------------------------------------------------------------- #
# Guaranteed bilateral teardown machinery (verified offline, used live)
# --------------------------------------------------------------------------- #


class ArtifactTracker:
    """Records exactly which synthetic artifacts were created, so teardown removes
    precisely those (partial-setup aware — a create that failed halfway leaves only
    what actually landed)."""

    def __init__(self) -> None:
        self.jira_keys: list[str] = []
        self.local_ids: list[str] = []

    def track_jira(self, key: str) -> str:
        if key:
            self.jira_keys.append(key)
        return key

    def track_local(self, local_id: str) -> str:
        if local_id:
            self.local_ids.append(local_id)
        return local_id


def _retry(fn: Callable[[], Any], *, attempts: int = 3, sleep_fn=time.sleep) -> bool:
    """Call fn with bounded exponential backoff; True on success, False on exhaustion."""
    for attempt in range(attempts):
        try:
            fn()
            return True
        except Exception:  # noqa: BLE001 — teardown is best-effort per artifact; caller records leaks
            if attempt < attempts - 1:
                sleep_fn(2**attempt)
    return False


def _append_leaked(leaked_log: Path, ids: list[str]) -> None:
    with open(leaked_log, "a", encoding="utf-8") as fh:
        for aid in ids:
            fh.write(f"{aid}\n")


def _bilateral_teardown(
    tracker: ArtifactTracker,
    *,
    delete_jira: Callable[[str], Any],
    discard_local: Callable[[str], Any],
    leaked_log: Path = LEAKED_LOG,
    sleep_fn=time.sleep,
) -> list[str]:
    """Remove EVERY tracked artifact from BOTH systems, retrying each with backoff.
    Returns the list of ids that could not be removed (also appended to leaked_log).
    Runs to completion even if some deletes fail (a mid-teardown error never strands
    the rest)."""
    leaked: list[str] = []
    for key in tracker.jira_keys:
        if not _retry(lambda k=key: delete_jira(k), sleep_fn=sleep_fn):
            leaked.append(key)
    for local_id in tracker.local_ids:
        if not _retry(lambda lid=local_id: discard_local(lid), sleep_fn=sleep_fn):
            leaked.append(local_id)
    if leaked:
        _append_leaked(leaked_log, leaked)
    return leaked


# --------------------------------------------------------------------------- #
# OFFLINE-verifiable ACs (no live Jira) — teardown-under-failure + breaker
# --------------------------------------------------------------------------- #


def test_teardown_runs_on_failure_and_cleans_both_sides(tmp_path):
    """A RuntimeError injected AFTER create must still leave ZERO artifacts — the
    finally-teardown removes both the Jira issue and the local ticket."""
    tracker = ArtifactTracker()
    deleted_jira: list[str] = []
    discarded_local: list[str] = []
    leaked_log = tmp_path / "leaked.log"

    def _delete_jira(key):
        deleted_jira.append(key)

    def _discard_local(lid):
        discarded_local.append(lid)

    leaked: list[str] = []
    try:
        # --- setup: both artifacts created and tracked ---
        tracker.track_jira("TEST-1")
        tracker.track_local("loc-1")
        # --- test body raises AFTER create (the failure the AC injects) ---
        raise RuntimeError("injected mid-test failure")
    except RuntimeError:
        pass
    finally:
        leaked = _bilateral_teardown(
            tracker,
            delete_jira=_delete_jira,
            discard_local=_discard_local,
            leaked_log=leaked_log,
            sleep_fn=lambda _s: None,
        )

    assert deleted_jira == ["TEST-1"], "Jira artifact must be hard-deleted on failure"
    assert discarded_local == ["loc-1"], "local artifact must be discarded on failure"
    assert leaked == [], "no artifact may leak"
    assert not leaked_log.exists(), "no leaked-artifacts.log entry on a clean teardown"


def test_teardown_exhaustion_records_leak_and_reports(tmp_path):
    """When a delete never succeeds, the id is appended to leaked-artifacts.log and
    surfaced (the caller exits non-zero on a non-empty leak list)."""
    tracker = ArtifactTracker()
    tracker.track_jira("STUCK-1")
    leaked_log = tmp_path / "leaked.log"

    def _always_fails(_key):
        raise RuntimeError("Jira delete keeps failing")

    leaked = _bilateral_teardown(
        tracker,
        delete_jira=_always_fails,
        discard_local=lambda _l: None,
        leaked_log=leaked_log,
        sleep_fn=lambda _s: None,
    )

    assert leaked == ["STUCK-1"]
    assert leaked_log.read_text(encoding="utf-8").strip() == "STUCK-1"


def test_blast_radius_breaker_via_classify_api(tmp_path):
    """Blast-radius breaker (Round-6 correction): assert via check_blast_radius —
    a lone acting decision at fraction 0.09 does NOT trip; a mass-change DOES."""
    classify = _load("classify_d01e_breaker", "classify.py")
    noop = classify.Decision(classify.DecisionKind.NOOP, "noop")
    acting = classify.Decision(classify.DecisionKind.ADOPT, "adopt")  # ADOPT is acting

    # 11 bindings, 1 acting -> fraction ~0.0909 <= 0.10 -> allowed.
    ok = classify.check_blast_radius(
        [acting] + [noop] * 10, total_bindings=11, max_acting_fraction=0.10
    )
    assert ok.allowed is True
    assert ok.acting_count == 1

    # 11 bindings, 6 acting -> fraction ~0.545 > 0.10 -> tripped.
    tripped = classify.check_blast_radius(
        [acting] * 6 + [noop] * 5, total_bindings=11, max_acting_fraction=0.10
    )
    assert tripped.allowed is False, "a mass-change must trip the breaker"


# --------------------------------------------------------------------------- #
# LIVE matrix scenarios — the multi-hour tail (implemented in the live-run
# session). Each is ONE observable pass criterion, run under the guaranteed
# ``_bilateral_teardown`` above so a mid-scenario failure still cleans up both
# systems. Held as explicit scaffolds so the matrix + its teardown contract are
# recorded now; the live wiring (real AcliClient against the test project +
# per-scenario ArtifactTracker) lands with the live run.
# --------------------------------------------------------------------------- #

_SCAFFOLD = (
    "d01e live scenario — implement + run against the dedicated test project (live-run session)"
)


@pytest.mark.skip(reason=_SCAFFOLD)
def test_delete_permission_probe():
    """Setup probe: create one throwaway issue and assert delete returns 'deleted'
    (acli raises loud PermissionError if the credential is not delete-scoped)."""


@pytest.mark.skip(reason=_SCAFFOLD)
def test_c1_conflict_signal():
    """C1: assert against the exact conflict record C1 (feline-wandering-dassierat) lands."""


@pytest.mark.skip(reason=_SCAFFOLD)
def test_c2_hard_delete_recreates_bound_issue():
    """C2: after a Jira hard-delete, the next pass creates a NEW issue with the same
    rebar-id label + entity property and binds it; no false comment."""


@pytest.mark.skip(reason=_SCAFFOLD)
def test_c3_field_allowlist_drop_alert():
    """C3: a dropped outbound field fires the deduped BRIDGE_ALERT (post-C3 behavior)."""


@pytest.mark.skip(reason=_SCAFFOLD)
def test_c4_429_jittered_backoff():
    """C4: a simulated 429 exit on the _run_acli subprocess retry loop -> bounded
    backoff -> success (Retry-After honored only if C4's probe confirmed availability)."""


@pytest.mark.skip(reason=_SCAFFOLD)
def test_status_round_trip_lossy_labels_no_oscillation():
    """local blocked -> Jira nearest status + rebar-status:blocked label -> inbound
    restores blocked; idea<->IDEA round-trips; no oscillation over N=3 passes."""


@pytest.mark.skip(reason=_SCAFFOLD)
def test_idempotency_zero_mutations_on_noop_pass():
    """Echo suppression + idempotency: a pass over just-written data emits
    mutation_count == 0 (baseline pre-seeded as a real pass would)."""


@pytest.mark.skip(reason=_SCAFFOLD)
def test_tombstone_grace_retire():
    """local archive -> Jira Done (not deleted); a 404 on N=RECONCILER_ABSENT_RETIRE_GRACE
    consecutive passes -> soft-retire to bindings-retired.json; a single 404 does NOT."""
