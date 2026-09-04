"""Behavioral contract for one-pass plan relation snapshots."""

from __future__ import annotations

import importlib
import shutil
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import config

# Import ``generation`` at module scope so its ``from .relation_snapshot import
# collect_plan_relation_snapshot`` binding is captured from the REAL function before any
# test runs. Tests that ``monkeypatch.setattr(relation_snapshot,
# "collect_plan_relation_snapshot", ...)`` and then trigger generation's first import
# (via ``_run_plan_review``) would otherwise permanently capture the patched lambda into
# ``generation``'s namespace — a leak monkeypatch cannot revert.
from rebar.llm.plan_review import generation
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.pass1 import material_fingerprint


def _api():
    try:
        module = importlib.import_module("rebar.llm.plan_review.relation_snapshot")
    except ModuleNotFoundError:
        pytest.fail("plan relation snapshot API is absent")
    return (
        module.PlanRelationSnapshotError,
        module.collect_plan_relation_snapshot,
        module.tracker_head_sha,
    )


@pytest.fixture
def repo(tmp_path: Path) -> str:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    rebar.init_repo(repo_root=str(root))
    return str(root)


def _fingerprint(state: dict, children: list[dict] | None = None) -> str:
    return material_fingerprint(
        PlanContext(
            ticket_id=state["ticket_id"],
            ticket_type=state["ticket_type"],
            title=state["title"],
            description=state.get("description") or "",
            state=state,
            children=children or [],
        )
    )


def test_collects_canonical_children_and_both_prerequisite_directions(repo: str) -> None:
    _, collect_plan_relation_snapshot, _ = _api()
    subject = rebar.create_ticket("epic", "Subject", repo_root=repo)
    child = rebar.create_ticket("story", "Child", parent=subject, repo_root=repo)
    outgoing = rebar.create_ticket("task", "Outgoing prerequisite", repo_root=repo)
    incoming = rebar.create_ticket("task", "Archived inbound blocker", repo_root=repo)
    rebar.link(subject, outgoing[:8], "depends_on", repo_root=repo)
    rebar.link(incoming, subject, "blocks", repo_root=repo)
    rebar.archive(incoming, repo_root=repo)

    snapshot = collect_plan_relation_snapshot(subject, repo_root=repo)

    assert snapshot.child_ids == (child,)
    assert snapshot.prerequisite_ids == tuple(sorted((incoming, outgoing)))
    keys = [(pin.role, pin.canonical_id) for pin in snapshot.related_material]
    assert keys == sorted(
        [
            ("child", child),
            ("prerequisite", incoming),
            ("prerequisite", outgoing),
        ]
    )
    for pin in snapshot.related_material:
        target = snapshot.ticket_states_by_id[pin.canonical_id]
        target_children = [
            state
            for state in snapshot.ticket_states_by_id.values()
            if state.get("parent_id") == pin.canonical_id
        ]
        assert pin.material_fingerprint == _fingerprint(target, target_children)


def test_same_canonical_target_can_be_child_and_prerequisite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar.llm.plan_review import relation_snapshot

    subject = "1111-2222-3333-4444"
    dual = "aaaa-bbbb-cccc-dddd"
    tracker = tmp_path / "tracker"
    (tracker / subject).mkdir(parents=True)
    (tracker / dual).mkdir()
    states = [
        {
            "ticket_id": subject,
            "ticket_type": "epic",
            "title": "Subject",
            "description": "",
            "status": "open",
            "deps": [{"relation": "depends_on", "target_id": dual}],
        },
        {
            "ticket_id": dual,
            "ticket_type": "story",
            "title": "Dual",
            "description": "",
            "status": "open",
            "parent_id": subject,
            "deps": [],
        },
    ]
    calls = 0

    def reduce_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        return states

    monkeypatch.setattr(relation_snapshot.config, "tracker_dir", lambda _: str(tracker))
    monkeypatch.setattr(relation_snapshot, "tracker_head_sha", lambda _: "a" * 40)
    monkeypatch.setattr(relation_snapshot, "reduce_all_tickets", reduce_once)

    snapshot = relation_snapshot.collect_plan_relation_snapshot(subject, repo_root="ignored")

    assert calls == 1
    assert [(pin.role, pin.canonical_id) for pin in snapshot.related_material] == [
        ("child", dual),
        ("prerequisite", dual),
    ]


def test_store_preload_accepts_canonical_jira_local_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar.llm.plan_review import relation_snapshot

    subject = "jira-reb-1160"
    tracker = tmp_path / "tracker"
    (tracker / subject).mkdir(parents=True)
    state = {
        "ticket_id": subject,
        "ticket_type": "epic",
        "title": "Jira epic",
        "description": "",
        "status": "open",
        "deps": [],
    }
    monkeypatch.setattr(relation_snapshot.config, "tracker_dir", lambda _: str(tracker))
    monkeypatch.setattr(relation_snapshot, "tracker_head_sha", lambda _: "a" * 40)
    monkeypatch.setattr(relation_snapshot, "reduce_all_tickets", lambda *a, **k: [state])

    snapshot = relation_snapshot.collect_plan_relation_snapshot(subject, repo_root="ignored")

    assert snapshot.subject_state["ticket_id"] == subject


def test_snapshot_error_is_structured_unsigned_and_pre_llm(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from rebar.llm.config import LLMConfig
    from rebar.llm.plan_review import _run_plan_review, relation_snapshot

    class NeverRunner:
        def preflight(self):
            raise AssertionError("runner preflight must not execute")

        def run(self, request):
            raise AssertionError("runner must not execute")

    error = relation_snapshot.PlanRelationSnapshotError(
        "missing-target", canonical_id="aaaa-bbbb-cccc-dddd", reference="missing-ref"
    )
    monkeypatch.setattr(
        relation_snapshot,
        "collect_plan_relation_snapshot",
        lambda *a, **k: (_ for _ in ()).throw(error),
    )

    verdict = _run_plan_review(
        "1111-2222-3333-4444",
        cfg=LLMConfig(),
        runner=NeverRunner(),
        sign=True,
        emit_sidecar=True,
        advisory_cap=None,
        repo_root=None,
    )

    assert verdict["verdict"] == "INDETERMINATE"
    assert verdict["signature"] == {"signed": False, "reason": "missing-target"}
    assert verdict["coverage"]["llm_ran"] is False
    record = next(r for r in caplog.records if getattr(r, "event", None))
    assert {
        "event": record.event,
        "reason": record.reason,
        "canonical_id": record.canonical_id,
        "reference": record.reference,
    } == {
        "event": "plan_relation_snapshot_error",
        "reason": "missing-target",
        "canonical_id": "aaaa-bbbb-cccc-dddd",
        "reference": "missing-ref",
    }


def test_duplicate_edges_collapse_and_empty_description_is_readable(repo: str) -> None:
    _, collect_plan_relation_snapshot, _ = _api()
    subject = rebar.create_ticket("story", "Subject", repo_root=repo)
    target = rebar.create_ticket("task", "Target", description="", repo_root=repo)
    rebar.link(subject, target, "depends_on", repo_root=repo)
    rebar.link(subject, target[:8], "depends_on", repo_root=repo)

    snapshot = collect_plan_relation_snapshot(subject, repo_root=repo)

    matching = [pin for pin in snapshot.related_material if pin.canonical_id == target]
    assert len(matching) == 1
    assert matching[0].role == "prerequisite"


def test_deleted_target_is_missing_target(repo: str) -> None:
    PlanRelationSnapshotError, collect_plan_relation_snapshot, _ = _api()
    subject = rebar.create_ticket("story", "Subject", repo_root=repo)
    target = rebar.create_ticket("task", "Target", repo_root=repo)
    rebar.link(subject, target, "depends_on", repo_root=repo)
    tracker = Path(config.tracker_dir(repo))
    shutil.rmtree(tracker / target)
    subprocess.run(["git", "add", "-A"], cwd=tracker, check=True)
    subprocess.run(["git", "commit", "-qm", "remove target"], cwd=tracker, check=True)

    with pytest.raises(PlanRelationSnapshotError) as caught:
        collect_plan_relation_snapshot(subject, repo_root=repo)
    assert caught.value.reason == "missing-target"
    assert caught.value.reference == target


def test_tracker_head_sha_requires_clean_valid_git_head(repo: str) -> None:
    PlanRelationSnapshotError, _, tracker_head_sha = _api()
    tracker = Path(config.tracker_dir(repo))
    expected = subprocess.run(
        ["git", "-C", str(tracker), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    assert tracker_head_sha(str(tracker)) == expected
    assert len(expected) == 40 and expected == expected.lower()

    (tracker / "dirty.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(PlanRelationSnapshotError) as caught:
        tracker_head_sha(str(tracker))
    assert caught.value.reason == "store-read-failure"
    assert caught.value.reference == str(tracker)


@pytest.mark.parametrize("tracker", ["/definitely/missing/rebar-tracker", ""])
def test_tracker_head_sha_maps_path_and_subprocess_failures(tracker: str) -> None:
    PlanRelationSnapshotError, _, tracker_head_sha = _api()
    with pytest.raises(PlanRelationSnapshotError) as caught:
        tracker_head_sha(tracker)
    assert caught.value.reason == "store-read-failure"


def test_status_line_unmerged_entries_are_dirt() -> None:
    """Every unmerged X/Y porcelain code must fail the strict read: nothing in the
    canonical locked write path produces an unmerged index, so it is genuine dirt —
    while an index-only staged entry (clean worktree column) is a peer writer's
    normal footprint and must not be."""
    from rebar.llm.plan_review.relation_snapshot import _UNMERGED_XY, _status_line_is_dirt

    for xy in sorted(_UNMERGED_XY):
        assert _status_line_is_dirt(f"{xy} event.json"), xy
    assert not _status_line_is_dirt("A  staged-event.json")
    assert _status_line_is_dirt("AM staged-then-modified.json")
    assert _status_line_is_dirt("?? untracked.json")
    assert _status_line_is_dirt("A")


def test_tracker_head_sha_tolerates_peer_staged_index(repo: str) -> None:
    """Bug a83f site C: the canonical writer stages then commits as TWO subprocesses under
    its lock (``_store/event_append.py``); an unlocked reader landing in that gap sees an
    index-only staged entry with a CLEAN worktree column. That is another writer's normal
    footprint, not dirt — it must not collapse to ``store-read-failure``."""
    _, _, tracker_head_sha = _api()
    tracker = Path(config.tracker_dir(repo))
    expected = subprocess.run(
        ["git", "-C", str(tracker), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    (tracker / "staged-event.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "-C", str(tracker), "add", "staged-event.json"], check=True)
    assert tracker_head_sha(str(tracker)) == expected
    assert tracker_head_sha(str(tracker), ignore_untracked=True) == expected


def test_tracker_head_sha_still_fails_on_dirty_tracked_worktree(repo: str) -> None:
    """A WORKTREE-side modification to a tracked file is genuine dirt (nothing in the
    canonical write path produces it), so the strict read must keep failing closed."""
    PlanRelationSnapshotError, _, tracker_head_sha = _api()
    tracker = Path(config.tracker_dir(repo))
    tracked = tracker / "tracked.txt"
    tracked.write_text("v1", encoding="utf-8")
    subprocess.run(["git", "-C", str(tracker), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(tracker), "commit", "-qm", "add tracked"], check=True)
    tracked.write_text("v2", encoding="utf-8")
    for kwargs in ({}, {"ignore_untracked": True}):
        with pytest.raises(PlanRelationSnapshotError) as caught:
            tracker_head_sha(str(tracker), **kwargs)
        assert caught.value.reason == "store-read-failure"


def test_review_plan_preflight_tolerates_unrelated_untracked_tracker_files(repo: str) -> None:
    """Regression (bug d7cb-22ae): an unrelated untracked file left in the SHARED
    tickets-tracker by a crashed process on ANOTHER ticket must not collapse
    ``review-plan`` to INDETERMINATE/store-read-failure for every other ticket.

    The preflight relation snapshot is a READ that fingerprints the committed HEAD,
    which untracked files cannot change (the authoritative under-lock signing check
    already ignores them via ``ignore_untracked=True``), so the preflight must tolerate
    them. ``.tickets-tracker`` is symlinked into every session, so one stray artifact
    otherwise blocks review-plan — and therefore ``claim`` — machine-wide.
    """
    from rebar.llm.plan_review import review_plan

    subject_id = rebar.create_ticket("bug", "Preflight subject", description="x", repo_root=repo)
    tracker = Path(config.tracker_dir(repo))

    def review():
        return review_plan(
            subject_id, repo_root=repo, sign=False, emit_sidecar=False, runner=None, source="local"
        )

    def snapshot_reasons(verdict):
        return [entry.get("reason") for entry in (verdict.get("indeterminate") or [])]

    # Baseline: a CLEAN tracker never short-circuits on the preflight snapshot read.
    clean = review()
    assert "store-read-failure" not in snapshot_reasons(clean)

    # A crashed process left sidecar artifacts for a COMPLETELY UNRELATED ticket.
    (tracker / "6673-7636-a116-4f90-x-REVIEW_RESULT.json").write_text("{}", encoding="utf-8")
    (tracker / "6673-7636-a116-4f90-x-SIGNATURE.json").write_text("{}", encoding="utf-8")

    dirty = review()
    # The unrelated untracked files must NOT collapse this ticket's review to
    # store-read-failure — the observable outcome must match the clean-tracker run.
    assert "store-read-failure" not in snapshot_reasons(dirty), (
        "unrelated untracked tracker files collapsed review-plan to store-read-failure "
        "(shared-tracker blast radius not contained)"
    )
    assert dirty["verdict"] == clean["verdict"]


def test_sign_manifest_fence_tolerates_unrelated_untracked_tracker_files(
    repo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (bug d7cb-22ae, sibling on the SIGNING path): the generation
    stability fence (``before``/``fresh``/``after`` reads in ``sign_manifest``) must
    ignore unrelated untracked tracker files too, matching its own authoritative
    under-lock re-check (which already passes ``ignore_untracked=True``). The fence
    detects a concurrent COMMIT during generation — a moving committed HEAD, which
    untracked files cannot cause. Otherwise a stray artifact left by a crashed process
    on ANOTHER ticket aborts signing (``store-read-failure``), so no durable attestation
    is persisted and the plan-review claim gate cannot pass even for a clean plan.
    """
    monkeypatch.setenv("REBAR_SIGNING_KEY", "test-signing-key-2c2d")
    subject_id = rebar.create_ticket("bug", "Fence subject", description="x", repo_root=repo)

    # Snapshot the generation while the tracker is CLEAN (as the review would).
    initial = generation.collect(subject_id, repo_root=repo)

    # Only AFTER snapshotting, a crashed process leaves artifacts for a DIFFERENT ticket.
    tracker = Path(config.tracker_dir(repo))
    (tracker / "6673-7636-a116-4f90-x-REVIEW_RESULT.json").write_text("{}", encoding="utf-8")
    (tracker / "6673-7636-a116-4f90-x-SIGNATURE.json").write_text("{}", encoding="utf-8")

    # Must sign (not raise PlanReviewGenerationError/store-read-failure on the fence).
    signature = generation.sign_manifest(subject_id, ["m1", "m2"], initial, repo_root=repo)
    assert isinstance(signature, dict)
    assert signature.get("algorithm"), f"attestation not signed: {signature}"
    assert signature.get("ticket_id") == subject_id


def _resign_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A repo whose latest REVIEW_RESULT is a signable PASS, ready for ``resign_plan_review``.

    Mirrors the end-to-end setup in ``test_plan_review_resign.py`` (real repo + real store;
    only the sidecar read, the gate handle and the code sha are stubbed) so the assertion
    below exercises the REAL resign path rather than a mock of it.
    """
    import contextlib

    from rebar.llm import gate_source
    from rebar.llm.plan_review import resign

    root = tmp_path / "resignrepo"
    root.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "test@example.com"),
        ("git", "config", "user.name", "Test"),
        ("git", "commit", "-q", "--allow-empty", "-m", "initial"),
    ):
        subprocess.run(args, cwd=root, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(root))
    rebar.init_repo(repo_root=str(root))
    ticket_id = rebar.create_ticket("task", "resign subject", repo_root=str(root))
    rebar.declare_no_file_impact(ticket_id, "external operator action only", repo_root=str(root))

    material = generation.collect(ticket_id, repo_root=str(root)).own_material
    payload = {
        "verdict": "PASS",
        "ticket_id": ticket_id,
        "ticket_type": "task",
        "material_fingerprint": material,
        "coverage": {},
    }
    monkeypatch.setattr(resign.sidecar, "latest_review_result", lambda *a, **k: payload)
    monkeypatch.setattr(gate_source, "resolve_gate_handle", lambda *a, **k: object())
    monkeypatch.setattr(gate_source, "gate_read_root", lambda *a, **k: contextlib.nullcontext())
    code_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    monkeypatch.setattr("rebar.llm.gate_context.current_code_sha", lambda: code_head)
    monkeypatch.setenv("REBAR_SIGNING_KEY", "test-signing-key-c083")
    return resign, ticket_id, str(root)


def test_resign_tolerates_unrelated_untracked_tracker_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (bug c083): ``rebar sign-review`` must not abort because a crashed process
    left an UNRELATED untracked artifact in the SHARED tickets-tracker.

    This is the third site of the class bug ``d7cb-22ae`` fixed: that fix taught the review
    preflight (``__init__.py``) and the signing fence (``generation.py``) to pass
    ``ignore_untracked=True``, but missed ``resign.py``'s ``generation.collect`` — so the
    sanctioned recovery for an unsigned PASS still collapsed to ``store-read-failure``.

    The snapshot fingerprints the COMMITTED head, which an untracked file cannot change, and
    the authoritative under-lock re-check already ignores them (``generation.py``'s
    ``tracker_head_sha(..., ignore_untracked=True)``). ``.tickets-tracker`` is symlinked into
    every session, so one stray artifact otherwise blocks signing machine-wide.
    """
    resign, ticket_id, root = _resign_fixture(tmp_path, monkeypatch)
    tracker = Path(config.tracker_dir(root))

    # CONTROL: a clean tracker signs. Proves the fixture is valid, so a failure below is the
    # untracked file and not a broken setup.
    clean = resign.resign_plan_review(ticket_id, repo_root=root)
    assert clean["ok"] is True and clean["signed"] is True, f"clean-tracker control failed: {clean}"

    # A crashed process left an artifact for a COMPLETELY UNRELATED ticket. Deliberately NOT
    # named `.tmp-event-*`: the defect is the `git status --porcelain` untracked check, which
    # is content- and name-agnostic, so any untracked path reproduces it.
    (tracker / "zzz-unrelated-crash-artifact.json").write_text("{}", encoding="utf-8")

    dirty = resign.resign_plan_review(ticket_id, repo_root=root)
    assert "store-read-failure" not in str(dirty.get("reason") or ""), (
        "an unrelated untracked tracker file collapsed sign-review to store-read-failure "
        f"(shared-tracker blast radius not contained): {dirty}"
    )
    assert dirty["ok"] is True and dirty["signed"] is True, f"resign refused: {dirty}"


def test_resign_still_refuses_on_tracked_dirty_tracker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The c083 fix must NOT weaken the real guarantee: TRACKED dirty state in the tracker
    (a modified/staged committed file — which CAN change the fingerprinted head) must still
    refuse to sign. Only UNTRACKED files are tolerated."""
    resign, ticket_id, root = _resign_fixture(tmp_path, monkeypatch)
    tracker = Path(config.tracker_dir(root))

    tracked = next(iter(sorted(p for p in tracker.rglob("*.json") if p.is_file())), None)
    assert tracked is not None, "expected a committed tracker file to dirty"
    tracked.write_text(tracked.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    result = resign.resign_plan_review(ticket_id, repo_root=root)
    assert result["signed"] is False, f"tracked-dirty tracker must not sign: {result}"


def test_resign_tolerates_a_concurrent_writer_churning_tracker_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (bug c083, concurrency model): this machine runs MANY sessions against ONE
    shared ``.tickets-tracker`` (it is symlinked into every session), so untracked paths are
    not merely crash debris — they are the NORMAL transient state of another session's
    in-flight atomic write (write ``.tmp-event-<rand>``, then rename).

    With the strict check, signing was therefore a RACE: any session that signed while another
    was mid-write failed with ``store-read-failure``, and the failure rate scaled with
    concurrency. Tolerating untracked paths removes the race outright.

    NOTE a deliberate non-goal: the fix must NEVER clean up stray temp files. Deleting one is
    unsafe under this model — it could destroy another session's in-flight write. Tolerate,
    never tidy.
    """
    import threading

    resign, ticket_id, root = _resign_fixture(tmp_path, monkeypatch)
    tracker = Path(config.tracker_dir(root))

    # A steady-state artifact guarantees the pre-fix RED is deterministic rather than
    # dependent on winning a race with the churn thread below.
    (tracker / ".tmp-event-steadystate").write_text('{"partial":', encoding="utf-8")

    stop = threading.Event()

    def churn() -> None:
        i = 0
        while not stop.is_set():
            p = tracker / f".tmp-event-churn{i % 4}"
            try:
                p.write_text('{"in":"flight"', encoding="utf-8")
                p.unlink()
            except OSError:
                pass
            i += 1

    writer = threading.Thread(target=churn, daemon=True)
    writer.start()
    try:
        result = resign.resign_plan_review(ticket_id, repo_root=root)
    finally:
        stop.set()
        writer.join(timeout=5)

    assert "store-read-failure" not in str(result.get("reason") or ""), (
        "a concurrent session's in-flight temp writes collapsed sign-review to "
        f"store-read-failure — signing races other sessions: {result}"
    )
    assert result["ok"] is True and result["signed"] is True, f"resign refused: {result}"


def test_signer_and_gate_material_agree_across_archive(repo: str) -> None:
    """b7a2: the signer-side child enumeration (snapshot.child_ids, feeding
    ``generation.own_material``) and the gate-side enumeration
    (``attest.current_material_fingerprint``) must agree BOTH before and after a
    child is archived — that is the invariant whose violation makes a container
    permanently unclaimable. Before the fix the two diverge the moment a child is
    archived: the signer keeps it (``status != "deleted"``) while the claim gate
    drops it (``list_tickets`` default ``include_archived=False``)."""
    from rebar.llm.plan_review import attest, generation

    subject = rebar.create_ticket("epic", "Subject", repo_root=repo)
    rebar.create_ticket("story", "Live child", parent=subject, repo_root=repo)
    drop = rebar.create_ticket("story", "To be archived", parent=subject, repo_root=repo)

    def signer() -> str:
        return generation.collect(subject, repo_root=repo).own_material

    def gate() -> str | None:
        return attest.current_material_fingerprint(subject, repo_root=repo)

    assert signer() == gate(), "signer and gate disagree while all children are live"

    rebar.archive(drop, repo_root=repo)
    assert signer() == gate(), (
        "signer and gate diverged after archiving a child — the archived-vs-deleted "
        "predicate is spelled independently at the two sites (bug b7a2)"
    )


def test_archived_child_excluded_from_child_ids_and_pins(repo: str) -> None:
    """b7a2 AC1/AC5: an archived child is no longer plan material, so it must drop
    out of ``snapshot.child_ids`` and out of the ``plan-material-pin`` manifest, via
    the single shared predicate both sides use."""
    _, collect_plan_relation_snapshot, _ = _api()
    subject = rebar.create_ticket("epic", "Subject", repo_root=repo)
    keep = rebar.create_ticket("story", "Live child", parent=subject, repo_root=repo)
    drop = rebar.create_ticket("story", "To be archived", parent=subject, repo_root=repo)
    rebar.archive(drop, repo_root=repo)

    snapshot = collect_plan_relation_snapshot(subject, repo_root=repo)

    assert snapshot.child_ids == (keep,)
    child_pins = [pin.canonical_id for pin in snapshot.related_material if pin.role == "child"]
    assert drop not in child_pins
    assert child_pins == [keep]


def _capture_warnings(logger_name: str) -> tuple[list[str], object, object]:
    """Attach a handler DIRECTLY to ``logger_name``.

    ``caplog`` attaches to the root logger, so it silently misses records from a
    logger whose ``propagate`` has been turned off elsewhere in the process (the
    known ``configure_logging()`` leak). Binding the handler to the exact logger
    under test makes the assertion deterministic regardless of propagation.
    """
    import logging

    messages: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    logger = logging.getLogger(logger_name)
    handler = _Collect(level=logging.WARNING)
    logger.addHandler(handler)
    return messages, logger, handler


def test_event_less_ticket_directory_does_not_fail_an_unrelated_snapshot(repo: str) -> None:
    """Bug 043f: a ticket directory holding NO events — left behind when a write died
    between ``os.makedirs`` and the event rename — made ``_load_states`` raise
    ``reducer-error``, which surfaced as an unsigned INDETERMINATE verdict on plan
    reviews of completely UNRELATED tickets (the store reduction is store-WIDE, so every
    review in the clone failed until someone deleted the directory by hand).

    An event-less directory carries no relation material by construction, so the correct
    behaviour is to SKIP it with a warning naming the path, not to fail the whole
    snapshot. Note the artifact is invisible to git — git cannot track an empty directory
    — so no ``.gitignore`` remedy can reach it.
    """
    PlanRelationSnapshotError, collect_plan_relation_snapshot, _ = _api()
    subject = rebar.create_ticket("epic", "Unrelated subject", repo_root=repo)
    child = rebar.create_ticket("story", "Child", parent=subject, repo_root=repo)

    orphan = "0de5-6db1-8058-4e80"
    orphan_dir = Path(config.tracker_dir(repo)) / orphan
    orphan_dir.mkdir()
    assert not any(orphan_dir.iterdir()), "the artifact under test is an EMPTY directory"

    messages, logger, handler = _capture_warnings("rebar.llm.plan_review.relation_snapshot")
    try:
        snapshot = collect_plan_relation_snapshot(subject, repo_root=repo, ignore_untracked=True)
    except PlanRelationSnapshotError as exc:  # pragma: no cover — the RED path
        pytest.fail(
            "an event-less ticket directory failed the snapshot of an UNRELATED ticket: "
            f"reason={exc.reason} reference={exc.reference}"
        )
    finally:
        logger.removeHandler(handler)

    assert snapshot.child_ids == (child,), "the real relation material must be unaffected"
    assert orphan not in snapshot.ticket_states_by_id, "the event-less directory must be skipped"
    assert any(str(orphan_dir) in message for message in messages), (
        "the skip must be reported as a warning NAMING THE PATH so the artifact is "
        f"discoverable rather than silent; got: {messages}"
    )


def test_a_populated_but_unreducible_ticket_directory_still_fails_closed(repo: str) -> None:
    """Mutation guard for the opposite direction: the 043f tolerance must be narrow.

    A directory that DOES hold events but cannot be reduced (here: events with no CREATE)
    may carry relation material the snapshot would silently drop, so it must keep failing
    closed with ``reducer-error``. Only the genuinely event-less directory is skippable.
    """
    PlanRelationSnapshotError, collect_plan_relation_snapshot, _ = _api()
    subject = rebar.create_ticket("epic", "Unrelated subject", repo_root=repo)

    broken = "4ab3-411d-9736-4a41"
    broken_dir = Path(config.tracker_dir(repo)) / broken
    broken_dir.mkdir()
    (broken_dir / "1700000000000000000-aaaaaaaa-COMMENT.json").write_text(
        '{"event_type": "COMMENT", "uuid": "aaaaaaaa", "timestamp": 1700000000000000000,'
        ' "data": {"body": "no CREATE precedes me"}}',
        encoding="utf-8",
    )

    with pytest.raises(PlanRelationSnapshotError) as caught:
        collect_plan_relation_snapshot(subject, repo_root=repo, ignore_untracked=True)
    assert caught.value.reason == "reducer-error"
    assert caught.value.reference == broken
