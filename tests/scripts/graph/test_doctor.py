"""``doctor`` — classification, repair ordering, and the fail-safe paths.

The command finds blocking edges that predate the structural link rule (ticket
7ab3-9df0-7a90-4ffd) and optionally repairs them. The properties worth pinning are
less about the happy path than about what happens when a repair CANNOT complete:
no failure path may lose a dependency, and a pair that cannot be unlinked
relation-precisely must be declined rather than guessed at.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import ModuleType

import pytest
from _helpers import (
    _write_ticket,
)


def _tracker(tmp_path: Path) -> Path:
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    return tracker


def _event_count(tracker: Path) -> int:
    """Count EVENT files only.

    Reducing a ticket writes a `.cache.json` beside its events, so a plain
    `*/*.json` glob counts cache writes as if they were events and makes any
    "this wrote nothing" assertion fire on a read-only pass.
    """
    return len([p for p in tracker.glob("*/*.json") if not p.name.startswith(".")])


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_scan_reports_nothing_for_a_clean_store(graph: ModuleType, tmp_path: Path) -> None:
    """Sibling edges agree with the resolver, so nothing is reported."""
    from rebar._commands import doctor

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "story-parent", ticket_type="story")
    _write_ticket(tracker, "task-a", parent_id="story-parent", ticket_type="task")
    _write_ticket(tracker, "task-b", parent_id="story-parent", ticket_type="task")
    graph.add_dependency("task-a", "task-b", str(tracker), relation="depends_on")

    assert doctor.scan(str(tracker)) == []


@pytest.mark.unit
@pytest.mark.scripts
def test_scan_reports_nothing_when_there_are_no_blocking_edges(
    graph: ModuleType, tmp_path: Path
) -> None:
    """An empty dependency graph is clean, not an error."""
    from rebar._commands import doctor

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "lonely", ticket_type="task")

    assert doctor.scan(str(tracker)) == []


@pytest.mark.unit
@pytest.mark.scripts
def test_scan_classifies_an_epic_blocked_by_its_own_child(
    graph: ModuleType, tmp_path: Path
) -> None:
    """The bug 1803 shape on disk: an epic depending on its own child."""
    from rebar._commands import doctor

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "epic-e", ticket_type="epic")
    _write_ticket(tracker, "story-s", parent_id="epic-e", ticket_type="story")
    _seed_link(tracker, "epic-e", "story-s", "depends_on")

    findings = doctor.scan(str(tracker))

    assert len(findings) == 1, findings
    assert findings[0]["kind"] == "ancestor-blocking", findings[0]
    assert findings[0]["source"] == "epic-e"
    assert findings[0]["target"] == "story-s"


@pytest.mark.unit
@pytest.mark.scripts
def test_scan_classifies_a_cousin_edge_as_mis_escalated(graph: ModuleType, tmp_path: Path) -> None:
    """A cousin edge recorded under the old rule now resolves to the parents."""
    from rebar._commands import doctor

    tracker = _cousin_store(tmp_path)
    findings = doctor.scan(str(tracker))

    assert len(findings) == 1, findings
    assert findings[0]["kind"] == "mis-escalated", findings[0]
    assert findings[0]["resolved_source"] == "story-a"
    assert findings[0]["resolved_target"] == "story-b"


@pytest.mark.unit
@pytest.mark.scripts
def test_scan_ignores_non_blocking_relations(graph: ModuleType, tmp_path: Path) -> None:
    """Only blocks/depends_on are audited; the soft relations are never touched."""
    from rebar._commands import doctor

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "epic-e", ticket_type="epic")
    _write_ticket(tracker, "story-s", parent_id="epic-e", ticket_type="story")
    # An ancestor pair that WOULD be flagged were the relation blocking. Seeded raw:
    # add_dependency refuses a direct parent-child pair for ANY relation, because
    # is_redundant is computed from the original pair before the non-blocking return.
    _seed_link(tracker, "epic-e", "story-s", "relates_to")

    assert doctor.scan(str(tracker)) == []


@pytest.mark.unit
@pytest.mark.scripts
def test_scan_classifies_an_unreadable_endpoint(graph: ModuleType, tmp_path: Path) -> None:
    """An edge pointing at a ticket that no longer exists is reported, not raised."""
    from rebar._commands import doctor

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "task-a", ticket_type="task")
    _write_ticket(tracker, "task-b", ticket_type="task")
    graph.add_dependency("task-a", "task-b", str(tracker), relation="depends_on")

    import shutil

    shutil.rmtree(tracker / "task-b")

    findings = doctor.scan(str(tracker))
    assert len(findings) == 1, findings
    assert findings[0]["kind"] == "unreadable", findings[0]


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_repair_removes_an_ancestor_blocking_edge(graph: ModuleType, tmp_path: Path) -> None:
    """Repair unlinks the bad edge, and a second scan comes back clean."""
    from rebar._commands import doctor

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "epic-e", ticket_type="epic")
    _write_ticket(tracker, "story-s", parent_id="epic-e", ticket_type="story")
    _seed_link(tracker, "epic-e", "story-s", "depends_on")

    findings = doctor.scan(str(tracker))
    doctor.repair_finding(findings[0], str(tracker))

    assert findings[0]["repair_status"] == "repaired", findings[0]
    assert not graph._is_active_link("epic-e", "story-s", "depends_on", str(tracker))
    assert doctor.scan(str(tracker)) == []


@pytest.mark.unit
@pytest.mark.scripts
def test_repair_replaces_a_mis_escalated_edge(graph: ModuleType, tmp_path: Path) -> None:
    """Repair writes the resolved pair and removes the stale one."""
    from rebar._commands import doctor

    tracker = _cousin_store(tmp_path)
    findings = doctor.scan(str(tracker))
    doctor.repair_finding(findings[0], str(tracker))

    assert findings[0]["repair_status"] == "repaired", findings[0]
    assert graph._is_active_link("story-a", "story-b", "depends_on", str(tracker))
    assert not graph._is_active_link("leaf-a", "leaf-b", "depends_on", str(tracker))
    assert doctor.scan(str(tracker)) == []


@pytest.mark.unit
@pytest.mark.scripts
def test_repair_writes_the_replacement_before_removing_the_stale_edge(
    graph: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordering invariant: a failed unlink must never cost us the dependency.

    Unlink-first would leave nothing on disk to reconstruct the edge from. With
    link-first, the same failure leaves a SUPERSET — both edges present — which the
    next scan converges. This forces the unlink to raise and asserts nothing is lost.
    """
    from rebar._commands import doctor

    tracker = _cousin_store(tmp_path)
    findings = doctor.scan(str(tracker))

    def _boom(*_a, **_k):
        raise ValueError("unlink exploded")

    monkeypatch.setattr(doctor, "_unlink_edge", _boom)
    doctor.repair_finding(findings[0], str(tracker))

    assert findings[0]["repair_status"] == "unrepairable", findings[0]
    assert graph._is_active_link("story-a", "story-b", "depends_on", str(tracker)), (
        "the replacement must already be durable when the unlink fails"
    )
    assert graph._is_active_link("leaf-a", "leaf-b", "depends_on", str(tracker)), (
        "the original edge must survive a failed unlink — no dependency is lost"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_repair_is_resumable_from_the_superset_state(
    graph: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running repair over the interrupted superset converges to the replacement."""
    from rebar._commands import doctor

    tracker = _cousin_store(tmp_path)

    def _boom(*_a, **_k):
        raise ValueError("unlink exploded")

    original_unlink = doctor._unlink_edge
    monkeypatch.setattr(doctor, "_unlink_edge", _boom)
    doctor.repair_finding(doctor.scan(str(tracker))[0], str(tracker))

    # Restore by re-patching, NOT via monkeypatch.undo(): undo() reverts every patch
    # made through this fixture instance — including the git isolation conftest
    # installs — which would point the store at an uninitialized location.
    monkeypatch.setattr(doctor, "_unlink_edge", original_unlink)
    for finding in doctor.scan(str(tracker)):
        doctor.repair_finding(finding, str(tracker))

    assert graph._is_active_link("story-a", "story-b", "depends_on", str(tracker))
    assert not graph._is_active_link("leaf-a", "leaf-b", "depends_on", str(tracker))
    assert doctor.scan(str(tracker)) == []


@pytest.mark.unit
@pytest.mark.scripts
def test_repair_declines_a_pair_whose_unlink_would_cancel_another_relation(
    graph: ModuleType, tmp_path: Path
) -> None:
    """UNLINK is pair-scoped, so an ambiguous pair is declined, not guessed at.

    The pair carries a blocking edge AND a newer relates_to. Unlinking would cancel
    the relates_to, so repair must refuse and leave both links intact.
    """
    from rebar._commands import doctor

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "epic-e", ticket_type="epic")
    _write_ticket(tracker, "story-s", parent_id="epic-e", ticket_type="story")
    _seed_link(tracker, "epic-e", "story-s", "depends_on")
    _seed_link(tracker, "epic-e", "story-s", "relates_to", suffix="2")

    findings = [f for f in doctor.scan(str(tracker)) if f["kind"] == "ancestor-blocking"]
    assert findings, "the blocking edge should still be classified"
    doctor.repair_finding(findings[0], str(tracker))

    assert findings[0]["repair_status"] == "unrepairable", findings[0]
    assert "ambiguous-pair" in findings[0]["repair_reason"], findings[0]
    assert graph._is_active_link("epic-e", "story-s", "depends_on", str(tracker))
    assert graph._is_active_link("epic-e", "story-s", "relates_to", str(tracker))


@pytest.mark.unit
@pytest.mark.scripts
def test_repair_never_touches_an_unreadable_finding(graph: ModuleType, tmp_path: Path) -> None:
    """An unreadable edge is reported and left exactly as it was."""
    from rebar._commands import doctor

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "task-a", ticket_type="task")
    _write_ticket(tracker, "task-b", ticket_type="task")
    graph.add_dependency("task-a", "task-b", str(tracker), relation="depends_on")

    import shutil

    shutil.rmtree(tracker / "task-b")

    findings = doctor.scan(str(tracker))
    before = _event_count(tracker)
    doctor.repair_finding(findings[0], str(tracker))

    assert findings[0]["repair_status"] == "unrepairable", findings[0]
    assert findings[0]["repair_reason"] == "unreadable-endpoint"
    assert _event_count(tracker) == before, "an unreadable finding must write nothing"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _cousin_store(tmp_path: Path) -> Path:
    """Two leaves in different stories under one epic, linked directly.

    That direct edge is exactly what the old type-tier rule produced (both leaves
    were tier 0, so nothing escalated) and what the structural rule now resolves to
    the parent stories.
    """
    tracker = tmp_path / "tracker"
    tracker.mkdir(exist_ok=True)
    _write_ticket(tracker, "epic-root", ticket_type="epic")
    _write_ticket(tracker, "story-a", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker, "story-b", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker, "leaf-a", parent_id="story-a", ticket_type="task")
    _write_ticket(tracker, "leaf-b", parent_id="story-b", ticket_type="task")
    _seed_link(tracker, "leaf-a", "leaf-b", "depends_on")
    return tracker


def _seed_link(tracker: Path, source: str, target: str, relation: str, suffix: str = "1") -> None:
    """Write a raw LINK event, bypassing add_dependency's guards.

    The whole point of this command is edges the CURRENT rules would refuse, so the
    fixtures cannot be built through ``add_dependency`` — it rejects exactly these
    shapes. Writing the event directly is what a store predating the rule looks like.
    """
    event = {
        "event_type": "LINK",
        "uuid": f"link-{source}-{target}-{suffix}",
        "timestamp": 2000 + int(suffix),
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {"target_id": target, "relation": relation},
    }
    path = tracker / source / f"{2000 + int(suffix)}-link-{source}-{target}-{suffix}-LINK.json"
    path.write_text(json.dumps(event), encoding="utf-8")


@pytest.mark.unit
@pytest.mark.scripts
def test_repair_force_writes_the_pre_repair_tag_and_repoints_it(
    graph: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--repair` pre-tags the pre-run OID, and a SECOND run RE-POINTS the tag.

    The tag is the run's rollback anchor, so it must be written before the first
    write. It is force-written (``git tag -f``) precisely so a resumed or repeated
    repair re-anchors to that run's starting state instead of failing because the
    tag already exists.

    This fixture builds a REAL git-backed tracker. The shared synthetic tracker is
    not a git repo, so ``rev-parse HEAD`` returns empty, ``_pre_tag`` short-circuits
    and never tags — which would make every assertion here compare "" to "" and pass
    vacuously.
    """
    from rebar._commands import doctor
    from rebar._store.gitutil import run_git

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "epic-e", ticket_type="epic")
    _write_ticket(tracker, "story-s", parent_id="epic-e", ticket_type="story")
    _seed_link(tracker, "epic-e", "story-s", "depends_on")

    for args in (
        ("init", "-q"),
        ("config", "user.email", "t@example.invalid"),
        ("config", "user.name", "T"),
        ("add", "-A"),
        ("commit", "-q", "-m", "seed"),
    ):
        cp = run_git(str(tracker), *args, check=False)
        assert cp.returncode == 0, (args, cp.stderr)

    monkeypatch.setattr(doctor, "_reconciler_in_flight", lambda *_a, **_k: False)

    def _tag_oid() -> str:
        # `git rev-parse <unknown-ref>` echoes the ref name to stdout and exits
        # non-zero, so the return code — not the output — is what says "absent".
        cp = run_git(
            str(tracker), "rev-parse", "--verify", "-q", doctor.PRE_REPAIR_TAG, check=False
        )
        return cp.stdout.strip() if cp.returncode == 0 else ""

    def _head() -> str:
        return run_git(str(tracker), "rev-parse", "HEAD", check=False).stdout.strip()

    assert _head(), "fixture precondition: the tracker must be a real git repo"
    assert not _tag_oid(), "the tag must not exist before any repair run"

    pre_oid_1 = _head()
    _f1, reported_1 = doctor.run_repair(doctor.scan(str(tracker)), str(tracker))
    assert reported_1 == pre_oid_1, (reported_1, pre_oid_1)
    assert _tag_oid() == pre_oid_1, "tag must be written at the pre-run OID"

    # Advance HEAD, so a non-forcing `git tag` would leave the tag stale and the
    # re-point assertion below is not a tautology.
    cp = run_git(str(tracker), "commit", "-q", "--allow-empty", "-m", "advance", check=False)
    assert cp.returncode == 0, cp.stderr
    pre_oid_2 = _head()
    assert pre_oid_2 != pre_oid_1, "fixture precondition: HEAD must move between runs"

    _seed_link(tracker, "epic-e", "story-s", "depends_on", suffix="9")
    _f2, reported_2 = doctor.run_repair(doctor.scan(str(tracker)), str(tracker))
    assert reported_2 == pre_oid_2, (reported_2, pre_oid_2)
    assert _tag_oid() == pre_oid_2, "a second run must RE-POINT the tag, not fail"


@pytest.mark.unit
@pytest.mark.scripts
@pytest.mark.parametrize(
    ("rest", "expect_init_only"),
    [([], True), (["--repair"], False)],
)
def test_doctor_arm_reconverges_only_for_repair(
    monkeypatch: pytest.MonkeyPatch, rest: list[str], expect_init_only: bool
) -> None:
    """A plain `doctor` must not reconverge the store; only `--repair` may.

    `doctor` deliberately does NOT join `_WRITES_FULL`, whose arms reconverge on
    every invocation — that would make a read-only audit mutate/​sync the store. Its
    own arm passes ``init_only=True`` unless ``--repair`` is present.
    """
    from rebar import _cli

    seen: list[bool] = []
    monkeypatch.setattr(
        _cli, "ensure_initialized", lambda *_a, **kw: seen.append(kw.get("init_only"))
    )
    monkeypatch.setattr("rebar._commands.doctor.doctor_cli", lambda *_a, **_k: 0)

    _cli._dispatch("doctor", rest)

    assert seen == [expect_init_only], (rest, seen)


def _git_backed(tracker: Path) -> None:
    """Make `tracker` a real git repo so the store write path actually locks.

    Without this the synthetic tracker is not a git repo, `stage_and_commit` bails
    before taking the write lock, and any test about locking passes vacuously.
    """
    from rebar._store.gitutil import run_git

    for args in (
        ("init", "-q"),
        ("config", "user.email", "t@example.invalid"),
        ("config", "user.name", "T"),
        ("add", "-A"),
        ("commit", "-q", "-m", "seed"),
    ):
        cp = run_git(str(tracker), *args, check=False)
        assert cp.returncode == 0, (args, cp.stderr)


@pytest.mark.unit
@pytest.mark.scripts
def test_run_repair_does_not_hold_a_lock_that_blocks_its_own_writes(
    graph: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_repair` must not hold the tracker write lock across its own writes.

    Every event write takes that lock for itself
    (append_event -> write_and_push -> stage_and_commit -> write_lock), and it is
    NOT re-entrant. An outer hold made each inner acquisition block for its full
    60s timeout and then fail, so a pass repaired NOTHING while serialising the
    tracker for every other writer — the observed production symptom was 18
    findings and zero progress in ten minutes.

    Against the pre-fix implementation this test fails twice over: the finding comes
    back `unrepairable` carrying a flock timeout, and the call takes ~60s per item.
    """
    from rebar._commands import doctor

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "epic-e", ticket_type="epic")
    _write_ticket(tracker, "story-s", parent_id="epic-e", ticket_type="story")
    _seed_link(tracker, "epic-e", "story-s", "depends_on")
    _git_backed(tracker)

    monkeypatch.setattr(doctor, "_reconciler_in_flight", lambda *_a, **_k: False)

    findings = doctor.scan(str(tracker))
    assert len(findings) == 1, findings

    started = time.monotonic()
    doctor.run_repair(findings, str(tracker))
    elapsed = time.monotonic() - started

    assert findings[0]["repair_status"] == "repaired", findings[0]
    assert "flock" not in str(findings[0].get("repair_reason") or ""), findings[0]
    # The pre-fix code spent a full 60s lock timeout here before failing.
    # timing: hang-guard — self-contention guard; 30s dwarfs the ~1s repair
    assert elapsed < 30, f"run_repair took {elapsed:.1f}s — it is contending with itself"
    assert not graph._is_active_link("epic-e", "story-s", "depends_on", str(tracker))


@pytest.mark.unit
@pytest.mark.scripts
def test_json_output_carries_the_documented_finding_fields(
    graph: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    """`--output json` findings carry kind/source/target/relation, and repair_status
    after a `--repair` pass.

    The schema-coverage guard drives this command against a CLEAN store, so its
    findings array is empty and the per-finding fields are never actually exercised.
    This asserts on real parsed keys and values.
    """
    from rebar._commands import composer, doctor

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "epic-e", ticket_type="epic")
    _write_ticket(tracker, "story-s", parent_id="epic-e", ticket_type="story")
    _seed_link(tracker, "epic-e", "story-s", "depends_on")
    _git_backed(tracker)

    monkeypatch.setattr(doctor, "tracker_dir", lambda _repo_root=None: tracker)
    monkeypatch.setattr(composer, "tracker_dir", lambda _repo_root=None: tracker)
    monkeypatch.setattr(doctor, "_reconciler_in_flight", lambda *_a, **_k: False)

    # ── read-only pass ────────────────────────────────────────────────────────
    rc = doctor.doctor_cli(["--output", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1, "exit 1 while findings are outstanding"
    assert payload["finding_count"] == 1, payload
    assert set(payload) >= {"findings", "finding_count", "pre_repair_tag_oid"}, payload

    finding = payload["findings"][0]
    assert set(finding) >= {"kind", "source", "target", "relation"}, finding
    assert finding["kind"] in {"ancestor-blocking", "mis-escalated", "unreadable"}, finding
    assert finding["kind"] == "ancestor-blocking", finding
    assert finding["source"] == "epic-e", finding
    assert finding["target"] == "story-s", finding
    assert finding["relation"] == "depends_on", finding
    assert "repair_status" not in finding, "a read-only pass must not report a repair"

    # ── repair pass ───────────────────────────────────────────────────────────
    rc = doctor.doctor_cli(["--repair", "--output", "json"])
    repaired = json.loads(capsys.readouterr().out)

    assert rc == 0, "exit 0 once nothing is outstanding"
    assert repaired["findings"][0]["repair_status"] == "repaired", repaired
    assert repaired["pre_repair_tag_oid"], "a repair pass records the rollback anchor"


@pytest.mark.unit
@pytest.mark.scripts
def test_repair_dry_run_reports_findings_but_writes_nothing(
    graph: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    """`--repair --dry-run` must still REPORT what it would fix, while writing nothing.

    The two halves matter independently: a dry run that silently reported nothing
    would look identical to a clean store, and one that wrote events would defeat
    the point of the flag.
    """
    from rebar._commands import composer, doctor

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "epic-e", ticket_type="epic")
    _write_ticket(tracker, "story-s", parent_id="epic-e", ticket_type="story")
    _seed_link(tracker, "epic-e", "story-s", "depends_on")
    _git_backed(tracker)

    monkeypatch.setattr(doctor, "tracker_dir", lambda _repo_root=None: tracker)
    monkeypatch.setattr(composer, "tracker_dir", lambda _repo_root=None: tracker)
    monkeypatch.setattr(doctor, "_reconciler_in_flight", lambda *_a, **_k: False)

    before = _event_count(tracker)
    rc = doctor.doctor_cli(["--repair", "--dry-run", "--output", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert _event_count(tracker) == before, "a dry run must write no events"
    assert rc == 1, "findings are still outstanding after a dry run"
    assert payload["finding_count"] == 1, payload
    assert payload["findings"][0]["kind"] == "ancestor-blocking", payload
    assert "repair_status" not in payload["findings"][0], "nothing was actually repaired"
    assert graph._is_active_link("epic-e", "story-s", "depends_on", str(tracker)), (
        "the offending edge must survive a dry run untouched"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_repair_refuses_while_a_reconciler_pass_is_in_flight(
    graph: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--repair` must refuse to write while a reconciler pass holds the pass lock.

    The reconciler rewrites ticket events itself, so repairing underneath a live
    pass risks interleaving two writers over the same edges. The guard fails
    CLOSED — an indeterminate lock state also refuses — so the assertion here is
    that nothing is written and the refusal is explicit, not that it merely
    returns.
    """
    from rebar._commands import doctor
    from rebar._commands._seam import CommandError

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "epic-e", ticket_type="epic")
    _write_ticket(tracker, "story-s", parent_id="epic-e", ticket_type="story")
    _seed_link(tracker, "epic-e", "story-s", "depends_on")
    _git_backed(tracker)

    monkeypatch.setattr(doctor, "_reconciler_in_flight", lambda *_a, **_k: True)

    findings = doctor.scan(str(tracker))
    assert len(findings) == 1, findings
    before = _event_count(tracker)

    with pytest.raises(CommandError, match="reconciler pass is in flight"):
        doctor.run_repair(findings, str(tracker))

    assert _event_count(tracker) == before, "a refused repair must write no events"
    assert graph._is_active_link("epic-e", "story-s", "depends_on", str(tracker)), (
        "the offending edge must be untouched when the repair is refused"
    )
    assert "repair_status" not in findings[0], "no finding may be marked repaired"


@pytest.mark.unit
@pytest.mark.scripts
def test_unrepairable_finding_leaves_its_edge_and_the_run_continues(
    graph: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    """One unrepairable finding must not cost the run its other repairs.

    `add_dependency` refuses a link whose source is closed, which is correct — a
    closed ticket depending on something is meaningless — so a mis-escalated edge
    whose RESOLVED source has since closed can never be rewritten. This is the real
    residual class on this repository's own tracker, not a hypothetical.

    Three things are asserted together, because any one alone would let a
    regression through: the unrepairable edge SURVIVES untouched, an unrelated
    finding in the SAME run is still repaired, and the exit status still reports
    outstanding work.
    """
    from rebar._commands import composer, doctor

    tracker = _tracker(tmp_path)
    # Unrepairable: task-d -> task-b resolves to story-c -> task-b, but story-c is closed.
    _write_ticket(tracker, "epic-a", ticket_type="epic")
    _write_ticket(tracker, "task-b", parent_id="epic-a", ticket_type="task")
    _write_ticket(tracker, "story-c", parent_id="epic-a", ticket_type="story", status="closed")
    _write_ticket(tracker, "task-d", parent_id="story-c", ticket_type="task")
    _seed_link(tracker, "task-d", "task-b", "depends_on")
    # Repairable: a plain ancestor-blocking edge in an unrelated tree.
    _write_ticket(tracker, "epic-e", ticket_type="epic")
    _write_ticket(tracker, "story-s", parent_id="epic-e", ticket_type="story")
    _seed_link(tracker, "epic-e", "story-s", "depends_on", suffix="2")
    _git_backed(tracker)

    monkeypatch.setattr(doctor, "tracker_dir", lambda _repo_root=None: tracker)
    monkeypatch.setattr(composer, "tracker_dir", lambda _repo_root=None: tracker)
    monkeypatch.setattr(doctor, "_reconciler_in_flight", lambda *_a, **_k: False)

    rc = doctor.doctor_cli(["--repair", "--output", "json"])
    payload = json.loads(capsys.readouterr().out)
    by_source = {f["source"]: f for f in payload["findings"]}

    blocked = by_source["task-d"]
    assert blocked["repair_status"] == "unrepairable", blocked
    assert "closed" in blocked["repair_reason"], blocked
    assert graph._is_active_link("task-d", "task-b", "depends_on", str(tracker)), (
        "an unrepairable edge must be left exactly as recorded"
    )

    assert by_source["epic-e"]["repair_status"] == "repaired", by_source["epic-e"]
    assert not graph._is_active_link("epic-e", "story-s", "depends_on", str(tracker)), (
        "the run must continue past the unrepairable finding and repair the rest"
    )

    assert rc == 1, "outstanding findings remain, so the exit status must say so"


@pytest.mark.unit
@pytest.mark.scripts
def test_no_flag_run_writes_no_events(
    graph: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    """A bare `rebar doctor` is a diagnostic and must not mutate the store.

    Asserted through `doctor_cli` rather than `scan()`, because the CLI is what a
    user actually invokes and the repair branch lives there — testing `scan()`
    alone would leave the flag routing unpinned.
    """
    from rebar._commands import composer, doctor

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "epic-e", ticket_type="epic")
    _write_ticket(tracker, "story-s", parent_id="epic-e", ticket_type="story")
    _seed_link(tracker, "epic-e", "story-s", "depends_on")
    _git_backed(tracker)

    monkeypatch.setattr(doctor, "tracker_dir", lambda _repo_root=None: tracker)
    monkeypatch.setattr(composer, "tracker_dir", lambda _repo_root=None: tracker)
    monkeypatch.setattr(doctor, "_reconciler_in_flight", lambda *_a, **_k: False)

    before = _event_count(tracker)
    rc = doctor.doctor_cli([])
    capsys.readouterr()

    assert _event_count(tracker) == before, "a no-flags run must write no events"
    assert rc == 1, "the finding is still outstanding"
    assert graph._is_active_link("epic-e", "story-s", "depends_on", str(tracker)), (
        "a diagnostic run must leave the offending edge in place"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_non_blocking_only_store_is_untouched_by_repair(
    graph: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    """A store whose only links are non-blocking reports nothing and survives `--repair`.

    The ancestor pair here WOULD be reported were the relation blocking, so this
    pins that the relation filter — not the hierarchy — is what excludes it.
    Compares the event-file LISTING, not just the count, so a same-count
    rewrite could not pass.
    """
    from rebar._commands import composer, doctor

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "epic-e", ticket_type="epic")
    _write_ticket(tracker, "story-s", parent_id="epic-e", ticket_type="story")
    for i, relation in enumerate(("relates_to", "supersedes", "discovered_from"), start=1):
        _seed_link(tracker, "epic-e", "story-s", relation, suffix=str(i))
    _git_backed(tracker)

    monkeypatch.setattr(doctor, "tracker_dir", lambda _repo_root=None: tracker)
    monkeypatch.setattr(composer, "tracker_dir", lambda _repo_root=None: tracker)
    monkeypatch.setattr(doctor, "_reconciler_in_flight", lambda *_a, **_k: False)

    def _listing() -> list[str]:
        return sorted(
            str(p.relative_to(tracker))
            for p in tracker.glob("*/*.json")
            if not p.name.startswith(".")
        )

    before = _listing()
    rc = doctor.doctor_cli(["--repair", "--output", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["finding_count"] == 0, payload
    assert rc == 0, "a clean store exits 0"
    assert _listing() == before, "--repair must not touch a store with no blocking edges"
