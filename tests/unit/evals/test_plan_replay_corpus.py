"""Tests for the replay corpus builder (``rebar.llm.evals.plan_replay.corpus``).

The builder enumerates plan_review_result_v1/v2 sidecars from a tickets-tracker git
history (mirroring the proven git-object-walk approach in
``docs/experiments/plan-review-gate/harnesses/mine_outcome_corpus.py``: event blobs at
``<ticket_id>/<ns_ts>-<uuid>-<TYPE>.json``, enumerated via ``git rev-list --objects
--all`` so compacted/deleted blobs are still recovered), reconstructs the at-review
material by replaying CREATE/EDIT events up to the sidecar's timestamp, and marks a row
``verified`` when the reconstructed material's fingerprint matches the sidecar's stored
``material_fingerprint`` — trying the SAME generation ladder ``attest._legacy_material_ok``
uses (current normalized, and three legacy candidates), so a sidecar signed under an
older normalization generation still verifies.

No live/billable call: everything here is pure git + the real ``pass1.material_fingerprint``
/ ``material_diff.material_basis`` functions — no LLM, no network.
"""

from __future__ import annotations

import json
import subprocess
import uuid as uuidlib
from pathlib import Path

import pytest

from rebar.llm.evals.plan_replay import corpus
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.pass1 import material_fingerprint

pytestmark = pytest.mark.unit

_TS_COUNTER = [1700000000000000000]


def _next_ts() -> int:
    """Monotonically increasing fake nanosecond timestamp, matching the store's own
    ``<ns_ts>-<uuid>-<TYPE>.json`` convention closely enough for ordering."""
    _TS_COUNTER[0] += 1_000_000_000
    return _TS_COUNTER[0]


def _run_git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


class TrackerBuilder:
    """Builds a minimal real git repo shaped like a rebar `.tickets-tracker`, so the
    corpus builder's real git-object-walk machinery runs against real refs/objects
    (per test-design.md's tier table: "Git refs, merging, reachability, data loss ->
    real temporary git repositories and refs")."""

    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        _run_git(path, "init", "-q")
        _run_git(path, "config", "user.email", "test@example.com")
        _run_git(path, "config", "user.name", "Test")

    def _write_event(self, ticket_id: str, ts: int, event_type: str, data: dict) -> None:
        d = self.path / ticket_id
        d.mkdir(parents=True, exist_ok=True)
        ev_uuid = str(uuidlib.UUID(int=ts % (2**128)))
        fname = f"{ts}-{ev_uuid}-{event_type}.json"
        (d / fname).write_text(json.dumps({"data": data}))

    def create(self, ticket_id: str, *, description: str, ts: int | None = None) -> int:
        ts = ts or _next_ts()
        self._write_event(
            ticket_id, ts, "CREATE", {"ticket_type": "story", "description": description}
        )
        _run_git(self.path, "add", "-A")
        _run_git(self.path, "commit", "-q", "-m", f"create {ticket_id}")
        return ts

    def edit(self, ticket_id: str, *, fields: dict, ts: int | None = None) -> int:
        ts = ts or _next_ts()
        self._write_event(ticket_id, ts, "EDIT", {"fields": fields})
        _run_git(self.path, "add", "-A")
        _run_git(self.path, "commit", "-q", "-m", f"edit {ticket_id}")
        return ts

    def review_result(self, ticket_id: str, *, data: dict, ts: int | None = None) -> int:
        ts = ts or _next_ts()
        self._write_event(ticket_id, ts, "REVIEW_RESULT", data)
        _run_git(self.path, "add", "-A")
        _run_git(self.path, "commit", "-q", "-m", f"review {ticket_id}")
        return ts

    def delete_from_tree(self, ticket_id: str, ts: int) -> None:
        """Remove a previously-committed event file from the tree (still recoverable
        from history) — simulates ``sidecar.prune``'s deletion of REVIEW_RESULT files."""
        matches = list((self.path / ticket_id).glob(f"{ts}-*-REVIEW_RESULT.json"))
        assert matches, f"no REVIEW_RESULT at ts={ts} to delete"
        _run_git(self.path, "rm", "-q", str(matches[0].relative_to(self.path)))
        _run_git(self.path, "commit", "-q", "-m", f"prune {ticket_id}")


def _ctx(
    ticket_id: str,
    description: str,
    *,
    file_impact=None,
    file_impact_scope=None,
    no_file_impact_reason=None,
    children=(),
) -> PlanContext:
    state = {"file_impact": file_impact or []}
    if file_impact_scope is not None:
        state["file_impact_scope"] = file_impact_scope
    if no_file_impact_reason is not None:
        state["no_file_impact_reason"] = no_file_impact_reason
    return PlanContext(
        ticket_id=ticket_id,
        ticket_type="story",
        title="T",
        description=description,
        state=state,
        children=[{"ticket_id": c} for c in children],
    )


# ── happy path ──────────────────────────────────────────────────────────────────
def test_build_corpus_verifies_intact_edit_history(tmp_path):
    """A ticket with intact CREATE+EDIT history reconstructs and verifies under the
    CURRENT (non-legacy) fingerprint generation."""
    tracker = TrackerBuilder(tmp_path / "store")
    ticket_id = "0000-0000-0000-0001"
    tracker.create(ticket_id, description="Initial plan text.")
    edit_ts = tracker.edit(ticket_id, fields={"description": "Revised plan text."})

    ctx = _ctx(ticket_id, "Revised plan text.")
    fp = material_fingerprint(ctx)
    review_ts = tracker.review_result(
        ticket_id,
        data={
            "schema": "plan_review_result_v2",
            "ticket_id": ticket_id,
            "ticket_type": "story",
            "verdict": "PASS",
            "impact_model_version": "plan-v5",
            "regver": "abc123",
            "verified_at_sha": "deadbeef",
            "provider_provenance": {"ran_model": "bedrock:us.anthropic.claude-opus-4-8"},
            "review_phase": "planning",
            "material_fingerprint": fp,
            "reviewed_related_material": [],
        },
    )
    assert edit_ts < review_ts

    manifest = corpus.build_corpus({"main": str(tracker.path)}, cache_dir=tmp_path / "cache")

    assert manifest["stores"]["main"] == 1
    assert manifest["schema_histogram"]["plan_review_result_v2"] == 1
    assert manifest["verdict_histogram"]["PASS"] == 1
    assert manifest["verified_count"] == 1
    assert manifest["unverified_count"] == 0
    assert manifest["verified_ratio"] == 1.0
    assert manifest["verified_by_generation"]["current"] == 1


# ── legacy generation ─────────────────────────────────────────────────────────
def test_build_corpus_verifies_under_legacy_generation(tmp_path):
    """A sidecar whose stored fingerprint was produced under the pre-330c (raw,
    unnormalized) algorithm still verifies — via the SAME candidate ladder
    ``attest._legacy_material_ok`` uses, not just the current normalization."""
    tracker = TrackerBuilder(tmp_path / "store")
    ticket_id = "0000-0000-0000-0002"
    description = "## AC\n- [x] done thing\n\n\n"
    tracker.create(ticket_id, description=description)

    ctx = _ctx(ticket_id, description)
    legacy_fp = material_fingerprint(ctx, normalize_checkboxes=False, normalize_reason=False)
    current_fp = material_fingerprint(ctx)
    assert legacy_fp != current_fp, "fixture description must differ across generations"

    tracker.review_result(
        ticket_id,
        data={
            "schema": "plan_review_result_v2",
            "ticket_id": ticket_id,
            "verdict": "PASS",
            "material_fingerprint": legacy_fp,
            "reviewed_related_material": [],
        },
    )

    manifest = corpus.build_corpus({"main": str(tracker.path)}, cache_dir=tmp_path / "cache")

    assert manifest["verified_count"] == 1
    assert manifest["verified_ratio"] == 1.0
    assert manifest["verified_by_generation"]["pre_330c"] == 1
    assert manifest["verified_by_generation"].get("current", 0) == 0


# ── conditional file_impact_scope component ────────────────────────────────────
def test_build_corpus_requires_no_file_impact_component(tmp_path):
    """A ticket that declared file_impact_scope=='none' only verifies when the
    reconstruction includes that conditional basis component (material_basis hashes
    it only under that condition) — omitting it must NOT spuriously verify."""
    tracker = TrackerBuilder(tmp_path / "store")
    ticket_id = "0000-0000-0000-0003"
    description = "Docs-only change, no source files touched."
    tracker.create(ticket_id, description=description)
    tracker.edit(
        ticket_id,
        fields={"file_impact_scope": "none", "no_file_impact_reason": "docs only"},
    )

    ctx = _ctx(
        ticket_id,
        description,
        file_impact_scope="none",
        no_file_impact_reason="docs only",
    )
    fp_with_scope = material_fingerprint(ctx)

    tracker.review_result(
        ticket_id,
        data={
            "schema": "plan_review_result_v2",
            "ticket_id": ticket_id,
            "verdict": "PASS",
            "material_fingerprint": fp_with_scope,
            "reviewed_related_material": [],
        },
    )

    manifest = corpus.build_corpus({"main": str(tracker.path)}, cache_dir=tmp_path / "cache")

    assert manifest["verified_count"] == 1
    assert manifest["verified_ratio"] == 1.0


# ── reconstruction is bounded to the review timestamp ──────────────────────────
def test_build_corpus_ignores_edits_after_the_review(tmp_path):
    """A LATER edit (made after the review it postdates) must NOT affect
    reconstruction — the builder replays only events up to the review's own
    timestamp, so the sidecar still verifies against its true at-review material
    despite the ticket's current (post-review) state having since diverged."""
    tracker = TrackerBuilder(tmp_path / "store")
    ticket_id = "0000-0000-0000-0004"
    tracker.create(ticket_id, description="Original text.")

    ctx_at_review = _ctx(ticket_id, "Original text.")
    fp_at_review = material_fingerprint(ctx_at_review)
    tracker.review_result(
        ticket_id,
        data={
            "schema": "plan_review_result_v2",
            "ticket_id": ticket_id,
            "verdict": "PASS",
            "material_fingerprint": fp_at_review,
            "reviewed_related_material": [],
        },
    )
    # A later edit changes the description AFTER the review was signed — must be
    # excluded from the reconstruction, not silently folded in.
    tracker.edit(ticket_id, fields={"description": "Materially different text."})

    manifest = corpus.build_corpus({"main": str(tracker.path)}, cache_dir=tmp_path / "cache")

    assert manifest["verified_count"] == 1
    assert manifest["unverified_count"] == 0
    assert manifest["verified_ratio"] == 1.0


# ── determinism ──────────────────────────────────────────────────────────────
def test_build_corpus_is_deterministic_across_rebuilds(tmp_path):
    """Two builds from the same store refs yield an identical manifest content hash
    (AC3) — rows sorted by a stable key and JSON serialized with sorted keys."""
    tracker = TrackerBuilder(tmp_path / "store")
    for i in range(3):
        ticket_id = f"0000-0000-0000-100{i}"
        tracker.create(ticket_id, description=f"Plan {i}.")
        ctx = _ctx(ticket_id, f"Plan {i}.")
        fp = material_fingerprint(ctx)
        tracker.review_result(
            ticket_id,
            data={
                "schema": "plan_review_result_v2",
                "ticket_id": ticket_id,
                "verdict": "PASS",
                "material_fingerprint": fp,
                "reviewed_related_material": [],
            },
        )

    manifest_1 = corpus.build_corpus({"main": str(tracker.path)}, cache_dir=tmp_path / "cache1")
    manifest_2 = corpus.build_corpus({"main": str(tracker.path)}, cache_dir=tmp_path / "cache2")

    assert manifest_1["content_hash"] == manifest_2["content_hash"]
    assert manifest_1["content_hash"]  # non-empty


# ── empty store ────────────────────────────────────────────────────────────────
def test_build_corpus_empty_store(tmp_path):
    """An empty store (no REVIEW_RESULT blobs anywhere in history) produces an empty
    manifest and does not raise."""
    tracker = TrackerBuilder(tmp_path / "store")
    # An empty git repo has no commits at all; give it one so `git rev-list` has a
    # ref to walk, but with no ticket/event content.
    (tracker.path / ".gitkeep").write_text("")
    _run_git(tracker.path, "add", "-A")
    _run_git(tracker.path, "commit", "-q", "-m", "empty")

    manifest = corpus.build_corpus({"main": str(tracker.path)}, cache_dir=tmp_path / "cache")

    assert manifest["stores"]["main"] == 0
    assert manifest["verified_count"] == 0
    assert manifest["unverified_count"] == 0
    assert manifest["row_count"] == 0


# ── unverified path ────────────────────────────────────────────────────────────
def test_build_corpus_marks_unverified_when_fingerprint_matches_nothing(tmp_path):
    """A sidecar whose stored material_fingerprint matches NO generation candidate
    (a genuinely garbled/foreign value) is marked unverified — verified_count stays 0,
    unverified_count increments, and the row is excluded from the written JSONL cache
    (per corpus.py's _write_cache, which writes verified rows only)."""
    tracker = TrackerBuilder(tmp_path / "store")
    ticket_id = "0000-0000-0000-0005"
    tracker.create(ticket_id, description="Some plan text.")
    tracker.review_result(
        ticket_id,
        data={
            "schema": "plan_review_result_v2",
            "ticket_id": ticket_id,
            "verdict": "PASS",
            "material_fingerprint": "not-a-real-fingerprint-at-all",
            "reviewed_related_material": [],
        },
    )

    cache_dir = tmp_path / "cache"
    manifest = corpus.build_corpus({"main": str(tracker.path)}, cache_dir=cache_dir)

    assert manifest["verified_count"] == 0
    assert manifest["unverified_count"] == 1
    assert manifest["verified_ratio"] == 0.0

    cache_file = cache_dir / f"{manifest['content_hash']}.jsonl"
    assert cache_file.exists()
    cached_rows = [json.loads(line) for line in cache_file.read_text().splitlines() if line.strip()]
    assert all(row["ticket_id"] != ticket_id for row in cached_rows), (
        "an unverified row must not appear in the verified-only JSONL cache"
    )


# ── history-recovered (deleted-from-tree) blobs ─────────────────────────────────
def test_build_corpus_recovers_a_review_result_deleted_from_the_tree(tmp_path):
    """The module's headline feature: a REVIEW_RESULT blob removed from the working
    tree (simulating sidecar.prune) is still recovered via the git-object walk
    (`git rev-list --objects --all`), which an on-disk scan would miss entirely."""
    tracker = TrackerBuilder(tmp_path / "store")
    ticket_id = "0000-0000-0000-0006"
    tracker.create(ticket_id, description="Pruned-sidecar plan text.")

    ctx = _ctx(ticket_id, "Pruned-sidecar plan text.")
    fp = material_fingerprint(ctx)
    review_ts = tracker.review_result(
        ticket_id,
        data={
            "schema": "plan_review_result_v2",
            "ticket_id": ticket_id,
            "verdict": "PASS",
            "material_fingerprint": fp,
            "reviewed_related_material": [],
        },
    )

    # Confirm the file is genuinely gone from the working tree before recovery.
    review_files_before = list((tracker.path / ticket_id).glob("*-REVIEW_RESULT.json"))
    assert review_files_before, "fixture setup: expected a REVIEW_RESULT file on disk"
    tracker.delete_from_tree(ticket_id, review_ts)
    review_files_after = list((tracker.path / ticket_id).glob("*-REVIEW_RESULT.json"))
    assert not review_files_after, "the REVIEW_RESULT file must be gone from the tree"

    manifest = corpus.build_corpus({"main": str(tracker.path)}, cache_dir=tmp_path / "cache")

    assert manifest["row_count"] == 1
    assert manifest["stores"]["main"] == 1
    assert manifest["verified_count"] == 1
    assert manifest["schema_histogram"]["plan_review_result_v2"] == 1
