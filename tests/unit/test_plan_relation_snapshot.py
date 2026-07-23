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
