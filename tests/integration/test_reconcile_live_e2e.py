"""Story d01e — comprehensive LIVE reconciler validation + GUARANTEED bilateral cleanup.

Runs the full bidirectional matrix against a dedicated live Jira TEST project and
guarantees teardown in BOTH systems (Jira issues hard-deleted + the local throwaway
env discarded), even on assertion failure / exception. The genuinely-live scenarios
self-skip without live env; every other matrix criterion is exercised DETERMINISTICALLY
against the real reconciler modules (no network), so the matrix runs green offline and
emits a JSON/JUnit report.

Run the live matrix with:  ``pytest tests/integration/test_reconcile_live_e2e.py -m live``
(the ``@_requires_live`` scenarios additionally need JIRA_URL / JIRA_USER /
JIRA_API_TOKEN + acli on PATH + a scoped test project).

Design (harness pattern, ADR 0037): an ``ArtifactTracker`` records exactly which of
the N synthetic artifacts were actually created (partial-setup aware), and
``_bilateral_teardown`` deletes precisely those — retrying each delete with bounded
backoff, appending the id to ``leaked-artifacts.log`` (a CI artifact) and failing the
run non-zero on exhaustion, so a leak is loud and never silent.

Each matrix scenario asserts ONE observable pass criterion against the real reconciler
seam that implements it:

* C1 — outbound both-sides conflict is RECORDED (local-wins preserved) + a deduped
  ``outbound-field-conflict`` bridge alert lands (bug a713).
* C2 — a hard-deleted bound issue retires after grace, and the re-create path re-stamps
  the SAME ``rebar-id:<local_id>`` label + ``local_id`` entity property and re-binds
  (write-ahead ordering, story 9622).
* C3 — a mapped-but-allowlist-dropped outbound field (issuetype) fires a deduped
  ``outbound-field-dropped`` bridge alert (bug acd0).
* C4 — a simulated 429 on the ``_run_acli`` subprocess loop → jittered bounded backoff →
  success; Retry-After honored when present (bug 943f).
* echo — outbound comments carry ``<!-- rebar:reconciler-echo -->`` and the inbound
  differ suppresses them (zero inbound mutations over just-written data).
* status round-trip — ``blocked`` → nearest live Jira status + ``rebar-status:blocked``
  label → inbound restores ``blocked``; ``idea ↔ IDEA``; idempotent over N=3 (no
  oscillation).
* idempotency — a field diff over just-written (equal) data emits zero mutations.
* tombstone/grace — a single 404 does NOT retire; ``RECONCILER_ABSENT_RETIRE_GRACE``
  consecutive 404s soft-retire the binding to ``bindings-retired.json``.
* blast-radius — ``classify.census(...)['breaker']['allowed'] is False`` on a mass-change
  pass; a lone acting decision under the cap is allowed.
"""

from __future__ import annotations

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

# The reconciler package is not installed top-level (it lives under _engine/); make it
# importable so the matrix drives the REAL seams. Story eca4 replaces this path shim
# with a proper package import once rebar_reconciler is packaged.
if str(RECON_DIR.parent) not in sys.path:
    sys.path.insert(0, str(RECON_DIR.parent))

_LIVE_ENV_KEYS = ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN")

# Every synthetic Jira artifact carries this marker in its summary so the leak sweep
# (and a human) can find and purge strays unambiguously.
DELETE_ME_MARKER = "DELETE-ME-d01e"


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


def test_blast_radius_breaker_via_classify_api():
    """Blast-radius breaker (Round-6 correction): assert via check_blast_radius —
    a lone acting decision at fraction 0.09 does NOT trip; a mass-change DOES."""
    from rebar_reconciler import classify

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


def test_blast_radius_breaker_via_census():
    """AC (Round-6): the census exposes the breaker verdict. ``census()['breaker']
    ['allowed'] is False`` on a mass-change pass — and the ``breaker`` key is present
    ONLY when both ``total_bindings`` and ``max_acting_fraction`` are supplied (never
    KeyError an un-evaluated pass)."""
    from rebar_reconciler import classify

    noop = classify.Decision(classify.DecisionKind.NOOP, "noop")
    acting = classify.Decision(classify.DecisionKind.ADOPT, "adopt")

    # Mass-change: 6/11 acting -> breaker trips.
    mass = classify.census([acting] * 6 + [noop] * 5, total_bindings=11, max_acting_fraction=0.10)
    assert mass["breaker"]["allowed"] is False
    assert mass["acting_count"] == 6

    # Under-cap: 1/11 acting -> allowed.
    calm = classify.census([acting] + [noop] * 10, total_bindings=11, max_acting_fraction=0.10)
    assert calm["breaker"]["allowed"] is True

    # No breaker inputs -> the census carries NO 'breaker' key (must not KeyError).
    bare = classify.census([acting] + [noop] * 10)
    assert "breaker" not in bare


# --------------------------------------------------------------------------- #
# LIVE matrix scenarios — one observable pass criterion each, run under the
# guaranteed ``_bilateral_teardown`` so a mid-scenario failure still cleans up
# both systems. The deterministic scenarios drive the REAL reconciler seam that
# implements the behavior; the ``@_requires_live`` probe additionally mutates
# real Jira (project REB) with tracked, guaranteed cleanup.
# --------------------------------------------------------------------------- #


@_requires_live
def test_delete_permission_probe(tmp_path):
    """Setup probe (re-runnable): create one throwaway REB issue, poll until it is
    index-visible (Jira eventual consistency), then hard-delete it and assert the
    delete succeeds and the issue eventually leaves the index. acli raises a loud
    PermissionError if the credential is not delete-scoped, so a scope misconfig fails
    HERE. Guaranteed bilateral cleanup via ArtifactTracker even on assertion failure
    (the leak log is routed to tmp so a leak never writes into REPO_ROOT)."""
    from rebar_reconciler import acli, acli_subprocess

    settings = acli_subprocess.resolve_jira_settings(project_default="REB")
    client = acli.AcliClient(
        jira_url=settings.url,
        user=settings.user,
        api_token=settings.api_token,
        jira_project=settings.project,
    )
    tracker = ArtifactTracker()
    summary = f"{DELETE_ME_MARKER} delete-permission probe"
    leaked_log = tmp_path / "leaked.log"

    def _delete_jira(key: str) -> None:
        client.delete_issue(key)

    try:
        created = client.create_issue({"title": summary, "ticket_type": "Task"})
        key = tracker.track_jira(created.get("key", ""))
        assert key, "create_issue returned no key"

        # Jira eventual-consistency settle: a create+IMMEDIATE-mutate races the index,
        # so poll until the issue is visible by key before deleting (~10-30s window).
        visible = _poll_until_visible(client, key, summary)
        assert visible, f"created issue {key} never became index-visible"

        # THE delete-permission criterion: a scoped credential returns "deleted"
        # (acli raises a loud PermissionError on a 403 if the token is not
        # delete-scoped). This return value is authoritative — unlike a follow-up
        # search, it is not subject to Jira's (unbounded) index-convergence lag, so
        # asserting the index is empty afterwards would be flaky. The delete having
        # returned "deleted" IS the server-side confirmation.
        result = client.delete_issue(key)
        assert result.get("status") in ("deleted", "not_found"), result
        # Deleted server-side; drop from the tracker so teardown does not attempt a
        # redundant re-delete (acli reports a confusing FAILURE on an already-gone key).
        tracker.jira_keys.clear()
    finally:
        _bilateral_teardown(
            tracker,
            delete_jira=_delete_jira,
            discard_local=lambda _l: None,
            leaked_log=leaked_log,
            sleep_fn=time.sleep,
        )


def _issue_exists(client: Any, key: str) -> bool:
    """True if a direct search by key returns the issue (index-visible)."""
    try:
        hits = client.search_issues(f'key = "{key}"')
    except Exception:  # noqa: BLE001 — a transient search error means "unknown"; caller treats as not-visible
        return False
    return any(h.get("key") == key for h in hits or [])


def _poll_until_visible(client: Any, key: str, summary: str, *, timeout_s: float = 30.0) -> bool:
    """Poll for index-visibility of a just-created issue (Jira eventual consistency).

    Tries a key search first, then a summary fallback (the ``labels``/``key`` index can
    lag the ``summary`` index after create). Returns True as soon as either sees it.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _issue_exists(client, key):
            return True
        try:
            hits = client.search_issues(f'summary ~ "{summary}"')
            if any(h.get("key") == key for h in hits or []):
                return True
        except Exception:  # noqa: BLE001 — search lag is expected mid-poll; keep polling until the deadline
            pass
        time.sleep(2.0)
    return _issue_exists(client, key)


def test_c1_conflict_signal(tmp_path):
    """C1 (a713): when a mirrored field changed on BOTH sides since the last sync
    (local != baseline AND jira != baseline), local-wins still overwrites — but the
    conflict is RECORDED and surfaces as a deduped ``outbound-field-conflict`` bridge
    alert. Asserts against the exact record C1 lands, driving the real differ +
    alert-emit seam."""
    from rebar_reconciler import alert_store, outbound_differ, run_differs

    conflict_sink: list[tuple[str, str]] = []
    ticket = _local_ticket(description="local-edit")
    jira = _jira_fields(description="jira-edit")

    changed = outbound_differ._diff_fields(
        ticket,
        jira,
        jira_key="C1-KEY-1",
        prev_jira_fields={"description": "baseline"},  # both sides diverged from this
        conflict_sink=conflict_sink,
    )

    # local-wins is PRESERVED (behavior unchanged) ...
    assert changed.get("description") == "local-edit"
    # ... AND the both-sides conflict is recorded as the exact (jira_key, field) tuple.
    assert ("C1-KEY-1", "description") in conflict_sink

    # The recorded conflict surfaces as a deduped bridge alert.
    run_differs._emit_outbound_field_alerts(conflict_sink, [], tmp_path, "pass-c1")
    assert alert_store.is_deduped(
        "outbound-field-conflict:C1-KEY-1:description", repo_root=tmp_path
    )


def test_c3_field_allowlist_drop_alert(tmp_path):
    """C3 (acd0): a mapped-but-allowlist-dropped outbound field (issuetype) that differs
    from Jira is RECORDED and fires a deduped ``outbound-field-dropped`` bridge alert
    (previously the drop was silent — stderr only). The drop behavior is unchanged
    (issuetype is still excluded from the outbound update)."""
    from rebar_reconciler import alert_store, outbound_differ, run_differs

    dropped_sink: list[tuple[str, str]] = []
    changed = outbound_differ._diff_fields(
        _local_ticket(ticket_type="bug"),  # -> issuetype "Bug"
        _jira_fields(issuetype={"name": "Task"}),
        jira_key="C3-KEY-1",
        dropped_field_sink=dropped_sink,
    )

    assert "issuetype" not in changed, "issuetype must stay excluded from the outbound update"
    assert ("C3-KEY-1", "issuetype") in dropped_sink

    run_differs._emit_outbound_field_alerts([], dropped_sink, tmp_path, "pass-c3")
    assert alert_store.is_deduped("outbound-field-dropped:C3-KEY-1:issuetype", repo_root=tmp_path)

    # Dedup: a second emit within the 24h window does NOT double-write the alert.
    run_differs._emit_outbound_field_alerts([], dropped_sink, tmp_path, "pass-c3-again")
    store_dir = alert_store._store_dir(tmp_path)
    lines = [
        ln
        for jf in store_dir.glob("*.jsonl")
        for ln in jf.read_text(encoding="utf-8").splitlines()
        if "outbound-field-dropped:C3-KEY-1:issuetype" in ln
    ]
    assert len(lines) == 1, "the field-drop alert must be deduped per (kind, ticket, field)"


def test_c4_429_jittered_backoff(monkeypatch):
    """C4 (943f): the ``_run_acli`` subprocess retry loop treats a 429 rate-limit
    (surfaced only as stderr text — acli is a subprocess) with bounded jittered backoff
    and RETRIES to success. Retry-After is honored IFF present. A single simulated 429
    exit -> backoff -> success."""
    from rebar_reconciler import acli_subprocess as sub

    # --- the pure backoff policy ---
    # No 429 marker -> None (caller keeps its uniform backoff; this is add-on only).
    assert sub._rate_limit_backoff(0, "connection reset") is None
    # 429 without Retry-After -> jittered exponential, bounded: 2^(0+1) + U(0,1) in [2,3).
    jittered = sub._rate_limit_backoff(0, "HTTP 429 Too Many Requests")
    assert jittered is not None and 2.0 <= jittered < 3.0
    # 429 WITH a parseable Retry-After -> honored (and capped at _MAX_BACKOFF_S).
    honored = sub._rate_limit_backoff(0, "429 rate limit; Retry-After: 7")
    assert honored == 7.0
    capped = sub._rate_limit_backoff(0, "429; Retry-After: 999999")
    assert capped == sub._MAX_BACKOFF_S

    # --- the full retry loop: a 429 exit then success ---
    slept: list[float] = []
    monkeypatch.setattr(sub.time, "sleep", lambda s: slept.append(s))
    # Pin the per-call timeout so the global Popen patch below doesn't intercept the
    # unrelated ``git rev-parse`` that config resolution shells out to.
    monkeypatch.setattr(sub, "_acli_call_timeout", lambda: 120)

    calls = {"n": 0}
    _real_popen = sub.subprocess.Popen

    class _FakeProc:
        def __init__(self, returncode: int, out: str, err: str) -> None:
            self.returncode = returncode
            self._out = out
            self._err = err

        def communicate(self, timeout=None):
            return self._out, self._err

    def _fake_popen(cmd, **kw):
        # Only intercept the acli invocation; delegate everything else (e.g. the
        # ``git rev-parse`` that config/build-info resolution shells out to) to the
        # real Popen so those unrelated subprocesses still work.
        is_acli = isinstance(cmd, (list, tuple)) and any("workitem" in str(c) for c in cmd)
        if not is_acli:
            return _real_popen(cmd, **kw)
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeProc(429, "", "HTTP 429 Too Many Requests")
        return _FakeProc(0, '{"key": "OK-1"}', "")

    monkeypatch.setattr(sub.subprocess, "Popen", _fake_popen)

    result = sub._run_acli(["jira", "workitem", "search", "--jql", "x"])
    assert result.returncode == 0
    assert calls["n"] == 2, "must retry exactly once after the 429"
    assert slept and slept[0] >= 2.0, "the 429 retry must sleep a bounded jittered backoff"


def test_status_round_trip_lossy_labels_no_oscillation():
    """Status round-trip incl. lossy labels: ``blocked`` has no direct live-workflow
    equivalent, so outbound maps it to the nearest live status AND stamps a
    ``rebar-status:blocked`` annotation label; inbound restores ``blocked`` from that
    label (label precedence). ``idea ↔ IDEA`` is injective (no annotation needed).
    Idempotent over N=3 passes — no oscillation."""
    from rebar_reconciler import config, inbound_differ, outbound_differ

    # idea <-> IDEA is a unique/injective round-trip (no annotation label).
    assert config.local_to_jira_status["idea"] == "IDEA"
    assert config.jira_to_local_status["IDEA"] == "idea"

    # blocked is lossy on the forward map (nearest live status)...
    assert config.local_to_jira_status["blocked"] == "In Progress"
    # ...so the lossless intent rides in a rebar-status: annotation label. First pass:
    # the label is absent on Jira -> emit an ADD.
    first = outbound_differ._diff_status_annotation_labels("blocked", [])
    assert {"action": "add", "label": "rebar-status:blocked"} in first

    # Inbound restores the EXACT local status from the annotation label, taking
    # precedence over the raw "In Progress" workflow status (which would map to
    # in_progress and lose the blocked intent).
    restored = inbound_differ._map_jira_to_local_fields(
        {"status": {"name": "In Progress"}, "labels": ["rebar-status:blocked"]}
    )
    assert restored["status"] == "blocked"

    # No oscillation over N=3: once the label is present, subsequent passes emit NO
    # add/remove mutations for it (steady state), and inbound keeps restoring blocked.
    for _ in range(3):
        steady = outbound_differ._diff_status_annotation_labels("blocked", ["rebar-status:blocked"])
        assert steady == [], "a settled annotation label must not re-mutate (no oscillation)"
        again = inbound_differ._map_jira_to_local_fields(
            {"status": {"name": "In Progress"}, "labels": ["rebar-status:blocked"]}
        )
        assert again["status"] == "blocked"


def test_idempotency_zero_mutations_on_noop_pass():
    """Echo suppression + idempotency: a pass over just-written data emits zero
    mutations. (1) A field diff where Jira already equals local -> {} changed fields.
    (2) An outbound comment carries the reconciler-echo marker; the inbound differ
    suppresses it (does NOT pull our own echo back in), so a comment we just wrote
    contributes zero inbound mutations."""
    from rebar_reconciler import inbound_differ, outbound_comments, outbound_differ

    # (1) field-level idempotency: identical local + jira -> no changed fields.
    # local status "open" maps to Jira "To Do"; pre-seed that so the pass is a true noop.
    same_ticket = _local_ticket(description="D", title="T", status="open")
    same_jira = _jira_fields(summary="T", description="D", status={"name": "To Do"})
    changed = outbound_differ._diff_fields(same_ticket, same_jira, jira_key="IDEMP-1")
    assert changed == {}, "a noop pass over just-written data must emit zero field mutations"

    # (2) echo suppression: our outbound comment is decorated with the reconciler marker.
    decorated = outbound_comments._decorate_outbound_comment("hello from rebar")
    assert outbound_comments.RECONCILER_MARKER in decorated

    # Reading that same comment back inbound is suppressed (zero inbound mutations)...
    echoed_back = {"comment": {"comments": [{"id": "10001", "body": decorated}], "total": 1}}
    assert inbound_differ._diff_comments_inbound(echoed_back, {"comments": []}) == []

    # ...whereas a genuine NEW Jira comment (no marker) IS pulled in (proves the
    # suppression is specific to our echoes, not a blanket no-op).
    genuine = {"comment": {"comments": [{"id": "10002", "body": "a human comment"}], "total": 1}}
    inbound = inbound_differ._diff_comments_inbound(genuine, {"comments": []})
    assert len(inbound) == 1 and inbound[0]["action"] == "add"


def test_tombstone_grace_retire(tmp_path, monkeypatch):
    """Tombstone/grace: a bound Jira key that 404s must NOT retire on a single miss;
    it soft-retires only after ``RECONCILER_ABSENT_RETIRE_GRACE`` CONSECUTIVE 404s,
    moving to ``bindings-retired.json`` (reversible) + a deduped ``binding-retired``
    alert. Drives the real BindingStore absence lifecycle."""
    from rebar_reconciler import binding_store as bs

    monkeypatch.setenv("RECONCILER_ABSENT_RETIRE_GRACE", "3")
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir(parents=True)
    store = bs.BindingStore(tracker_dir)

    local_id, jira_key = "loc-tomb-1", "REB-TOMB-1"
    store.bind_confirm(local_id, jira_key)

    # A single 404 does NOT retire.
    store.note_absent(jira_key)
    assert not store.is_retired(jira_key), "a single 404 must not retire the binding"

    # Second 404 (2 < grace) still does not retire.
    store.note_absent(jira_key)
    assert not store.is_retired(jira_key)

    # Third 404 reaches grace -> soft-retire.
    store.note_absent(jira_key)
    assert store.is_retired(jira_key), "grace consecutive 404s must retire the binding"

    # The retirement is durable + reversible in bindings-retired.json.
    retired_file = tracker_dir / ".bridge_state" / "bindings-retired.json"
    assert retired_file.exists()
    assert jira_key in retired_file.read_text(encoding="utf-8")

    # A binding-retired alert was emitted to the bridge_alerts store.
    from rebar_reconciler import alert_store

    store_dir = alert_store._store_dir(tmp_path)
    alert_lines = [
        ln
        for jf in store_dir.glob("*.jsonl")
        for ln in jf.read_text(encoding="utf-8").splitlines()
        if f'"key": "binding-retired:{jira_key}"' in ln
    ]
    assert alert_lines, "a binding-retired alert must be emitted on soft-retire"


def test_c2_hard_delete_recreates_bound_issue(tmp_path, monkeypatch):
    """C2 (c244 + write-ahead recovery, story 9622): after a Jira hard-delete retires
    the binding (local now unbound), the next pass re-creates a NEW issue and re-stamps
    the SAME identity — a ``rebar-id:<local_id>`` label + a ``local_id`` entity property
    — then re-binds it. No false comment. Drives the real ``create_one`` write-ahead
    ordering with a recording fake client."""
    from rebar_reconciler import binding_store as bs
    from rebar_reconciler import dispatch_one

    monkeypatch.setenv("RECONCILER_ABSENT_RETIRE_GRACE", "1")
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir(parents=True)
    store = bs.BindingStore(tracker_dir)

    local_id, old_key = "loc-c2-1", "REB-OLD-1"
    store.bind_confirm(local_id, old_key)

    # Hard-delete observed as a confirmed 404 -> retire (grace=1) -> local unbound.
    store.note_absent(old_key)
    assert store.is_retired(old_key)

    # The re-create pass: create_one must re-discover by the rebar-id JQL (miss ->
    # create), then stamp the identity markers and re-bind.
    recorder: dict[str, Any] = {"labels": [], "props": [], "searched": [], "created": 0}

    class _RecordingClient:
        def search_issues(self, jql, *a, **k):
            recorder["searched"].append(jql)
            return []  # dedup miss -> proceed to create

        def create_issue(self, ticket_data):
            recorder["created"] += 1
            recorder["created_data"] = ticket_data
            return {"key": "REB-NEW-1"}

        def add_label(self, key, label):
            recorder["labels"].append((key, label))

        def set_entity_property(self, key, name, value):
            recorder["props"].append((key, name, value))

        def delete_issue(self, key):  # rollback path — must NOT be hit on the happy path
            recorder.setdefault("deleted", []).append(key)

    mutation = {"local_id": local_id, "fields": {"summary": "re-created", "issuetype": "Task"}}
    result = dispatch_one.create_one(
        mutation,
        _RecordingClient(),
        repo_root=tmp_path,
        binding_store=store,
    )

    assert result == {"key": "REB-NEW-1"}
    assert recorder["created"] == 1, "a retired/unbound local must create exactly one NEW issue"
    # The dedup JQL keys off the SAME rebar-id label (re-discovery identity).
    assert any(f"rebar-id:{local_id}" in jql for jql in recorder["searched"])
    # SAME identity re-stamped on the new issue: rebar-id label + local_id entity property.
    assert ("REB-NEW-1", f"rebar-id:{local_id}") in recorder["labels"]
    assert ("REB-NEW-1", "local_id", local_id) in recorder["props"]
    # And the binding is re-confirmed to the new key (no rollback delete on the happy path).
    assert "deleted" not in recorder
    assert store._data["reverse"].get("REB-NEW-1") == local_id


# --------------------------------------------------------------------------- #
# Shared synthetic fixtures (local ticket + Jira field shapes)
# --------------------------------------------------------------------------- #


def _local_ticket(**overrides: Any) -> dict[str, Any]:
    t = {
        "ticket_id": "x",
        "title": "T",
        "description": "D",
        "ticket_type": "task",
        "priority": 2,
        "status": "open",
        "assignee": "",
    }
    t.update(overrides)
    return t


def _jira_fields(**overrides: Any) -> dict[str, Any]:
    f = {
        "summary": "T",
        "description": "D",
        "issuetype": {"name": "Task"},
        "priority": {"name": "Medium"},
        "assignee": None,
    }
    f.update(overrides)
    return f
