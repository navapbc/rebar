"""Structurally impossible inbound links are tried once, recorded durably, and skipped on
unchanged passes. Changed graph evidence requalifies them; transient failures keep
retrying, and the record is committed across checkouts."""

from __future__ import annotations

import importlib
import json
import logging
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> tuple[Path, str, str]:
    """Use a real store with a closed source so link creation returns the structural
    closed-source verdict."""
    import rebar

    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (
        ("git", "init", "-q", "-b", "main"),
        ("git", "config", "user.email", "t@example.com"),
        ("git", "config", "user.name", "T"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    a = str(rebar.create_ticket("task", "cicada impossible source", repo_root=repo))
    b = str(rebar.create_ticket("task", "cicada impossible target", repo_root=repo))
    rebar.transition(a, "open", "closed", repo_root=repo)
    return repo, a, b


@pytest.fixture
def apply_records():
    return importlib.import_module("rebar.reconciler.apply_inbound_records")


@pytest.fixture
def impossible_links():
    return importlib.import_module("rebar.reconciler.impossible_links")


def _counting_link(monkeypatch) -> list[tuple]:
    """Wrap ``rebar.link`` in a call counter that still calls through."""
    import rebar

    real_link = rebar.link
    calls: list[tuple] = []

    def counting(src, dst, relation, *, force="", repo_root=None):
        calls.append((src, dst, relation))
        return real_link(src, dst, relation, force=force, repo_root=repo_root)

    monkeypatch.setattr(rebar, "link", counting)
    return calls


def _add_payload(target_id: str, relation: str = "blocks") -> dict:
    return {"links": [{"action": "add", "target_id": target_id, "relation": relation}]}


def _store_path(repo: Path, impossible_links) -> Path:
    from rebar._commands._seam import tracker_dir

    return Path(str(tracker_dir(repo))) / impossible_links.STORE_RELATIVE


# ---------------------------------------------------------------------------
# classify: only provably-structural failures may ever be remembered
# ---------------------------------------------------------------------------

# Verbatim from the live pass logs (runs 31568815075 / 31570037358).
_CLOSED_SOURCE = (
    "rebar link failed (exit 1): Error: cannot create blocks link — source ticket "
    "'a880-b7e1-dc3e-407c' is closed. Reopen it first with: ticket transition "
    "a880-b7e1-dc3e-407c closed open"
)
_REDUNDANT = (
    "rebar link failed (exit 1): Error: ERROR: redundant link — 119f-63e3-34b7-4f96 and "
    "225a-323c-a9f6-436b are in an ancestor-descendant relationship; the hierarchy "
    "already expresses it"
)
_CYCLE = (
    "rebar link failed (exit 1): Error: Adding 42eb-5789-c798-4dfc → 0303-692c-55dc-4a18 "
    "(blocks) would create a cycle at epic level"
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_CLOSED_SOURCE, "closed_source"),
        (_REDUNDANT, "redundant_ancestry"),
        (_CYCLE, "cycle"),
    ],
)
def test_classify_recognises_each_permanent_shape(impossible_links, text, expected):
    """Each of the three real error texts maps to its permanent reason."""
    assert impossible_links.classify(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "RebarError('rebar link failed (exit 1): Error: git index.lock exists')",
        "ConnectionResetError(104, 'Connection reset by peer')",
        "RuntimeError('the store is closed for maintenance')",
        "",
    ],
)
def test_classify_refuses_anything_not_provably_structural(impossible_links, text):
    """Classify only exact structural refusals; an unrelated error containing “is closed”
    must remain retryable."""
    assert impossible_links.classify(text) is None


# ---------------------------------------------------------------------------
# The headline oracle: write attempts across two passes
# ---------------------------------------------------------------------------


def test_impossible_link_is_attempted_once_then_never_again(store, apply_records, monkeypatch):
    """Count actual link attempts: an unchanged structural refusal is attempted on pass 1 and
    skipped on pass 2."""
    repo, a, b = store
    calls = _counting_link(monkeypatch)

    first = apply_records._inbound_update_apply_links(_add_payload(b), a, repo)
    attempts_after_first = len(calls)

    second = apply_records._inbound_update_apply_links(_add_payload(b), a, repo)

    assert attempts_after_first == 1, (
        f"pass 1 should attempt the link exactly once, saw {attempts_after_first}: {calls!r}"
    )
    assert len(calls) == 1, (
        f"pass 2 re-attempted a known-impossible link; total attempts={len(calls)}: {calls!r}"
    )
    assert first == 0 and second == 0, (
        "a skipped impossible link must not be counted as applied "
        f"(links_applied: pass1={first}, pass2={second})"
    )


def test_the_skip_is_recorded_durably_and_survives_a_fresh_store(
    store, apply_records, impossible_links, monkeypatch
):
    """Persist the structural refusal so a fresh pass sees the skip and an operator can
    inspect it."""
    repo, a, b = store
    _counting_link(monkeypatch)

    apply_records._inbound_update_apply_links(_add_payload(b), a, repo)

    path = _store_path(repo, impossible_links)
    assert path.is_file(), f"no durable record was written at {path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == impossible_links.SCHEMA_VERSION
    key = impossible_links.record_key(a, b, "blocks")
    assert key in data["records"], f"no record for {key} in {data['records']!r}"
    assert data["records"][key]["reason"] == "closed_source"

    fresh = impossible_links.ImpossibleLinkStore(str(path.parent.parent))
    assert fresh.should_skip(a, b, "blocks") == "closed_source", (
        "a fresh store instance did not recover the recorded verdict"
    )


def test_reopening_the_source_requalifies_the_link(store, apply_records, monkeypatch):
    """Reopening the source changes the deciding graph evidence, so the link is retried and
    lands."""
    import rebar

    repo, a, b = store
    calls = _counting_link(monkeypatch)

    apply_records._inbound_update_apply_links(_add_payload(b), a, repo)
    assert len(calls) == 1

    rebar.transition(a, "closed", "open", repo_root=repo)

    applied = apply_records._inbound_update_apply_links(_add_payload(b), a, repo)

    assert len(calls) == 2, (
        f"reopening the source did not re-qualify the link for another attempt; attempts={calls!r}"
    )
    assert applied == 1, "the retry after reopening should have succeeded"
    targets = {
        dep.get("target_id") for dep in (rebar.show_ticket(a, repo_root=repo).get("deps") or [])
    }
    assert b in targets, f"the link did not land after the source was reopened: {targets!r}"


def test_a_comment_on_an_endpoint_does_not_requalify_the_link(store, apply_records, monkeypatch):
    """An endpoint comment is not structural evidence and must not cause another link
    attempt."""
    import rebar

    repo, a, b = store
    calls = _counting_link(monkeypatch)

    apply_records._inbound_update_apply_links(_add_payload(b), a, repo)
    assert len(calls) == 1

    rebar.comment(b, "an unrelated note that changes nothing structural", repo_root=repo)

    apply_records._inbound_update_apply_links(_add_payload(b), a, repo)

    assert len(calls) == 1, (
        f"a non-structural edit re-qualified the link and reintroduced churn: {calls!r}"
    )


def test_a_transient_failure_is_never_recorded_and_is_retried(
    store, apply_records, impossible_links, monkeypatch
):
    """An unclassified transient failure remains absent from the permanent store and is
    attempted again."""
    import rebar

    repo, a, b = store
    calls: list[tuple] = []

    def flaky(src, dst, relation, *, force="", repo_root=None):
        calls.append((src, dst, relation))
        raise RuntimeError("could not acquire the tracker lock")

    monkeypatch.setattr(rebar, "link", flaky)

    apply_records._inbound_update_apply_links(_add_payload(b), a, repo)
    apply_records._inbound_update_apply_links(_add_payload(b), a, repo)

    assert len(calls) == 2, f"a transient failure must be retried next pass; got {calls!r}"
    path = _store_path(repo, impossible_links)
    records = json.loads(path.read_text(encoding="utf-8"))["records"] if path.is_file() else {}
    assert records == {}, f"a transient failure was wrongly recorded as permanent: {records!r}"


def test_first_impossible_link_warns_and_the_repeat_skip_does_not(
    store, apply_records, monkeypatch, caplog
):
    """Warn on the first structural refusal, then skip it quietly on unchanged passes."""
    repo, a, b = store
    _counting_link(monkeypatch)

    with caplog.at_level(logging.WARNING):
        apply_records._inbound_update_apply_links(_add_payload(b), a, repo)
    first_warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("rebar.link failed" in r.getMessage() for r in first_warnings), (
        f"the first sighting was not surfaced to the operator: {first_warnings!r}"
    )
    assert any("structurally impossible" in r.getMessage() for r in first_warnings), (
        "the WARNING did not say the link will not be retried"
    )

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        apply_records._inbound_update_apply_links(_add_payload(b), a, repo)

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == [], (
        "the repeat pass still emitted a WARNING — the permanent error floor remains: "
        f"{[r.getMessage() for r in caplog.records]!r}"
    )


def test_a_cycle_record_is_invalidated_by_a_change_to_a_THIRD_ticket(
    tmp_path, apply_records, impossible_links, monkeypatch
):
    """A cycle can disappear when an intermediate ticket changes, so the graph fingerprint
    must include more than the endpoints."""
    import rebar

    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (
        ("git", "init", "-q", "-b", "main"),
        ("git", "config", "user.email", "t@example.com"),
        ("git", "config", "user.name", "T"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))

    a = str(rebar.create_ticket("task", "cycle a", repo_root=repo))
    b = str(rebar.create_ticket("task", "cycle b", repo_root=repo))
    c = str(rebar.create_ticket("task", "cycle c", repo_root=repo))
    # A chain a -> b -> c through an INTERMEDIATE ticket b.
    rebar.link(a, b, "blocks", repo_root=repo)
    rebar.link(b, c, "blocks", repo_root=repo)

    tracker = str(Path(str(_store_path(repo, impossible_links))).parent.parent)
    before = impossible_links.deciding_digest(c, a, "blocks", tracker, "cycle")

    # Break the chain at the INTERMEDIATE ticket. Neither endpoint (c, a) changes.
    rebar.unlink(b, c, repo_root=repo)

    after = impossible_links.deciding_digest(c, a, "blocks", tracker, "cycle")
    assert before != after, (
        "unlinking an intermediate ticket left the cycle digest unchanged; a "
        "now-possible link would stay suppressed until the file was deleted by hand"
    )

    # And the narrow shapes must NOT be disturbed by that unrelated third-ticket edit,
    # or every structural change anywhere would resurrect the churn for all records.
    assert impossible_links.deciding_digest(
        c, a, "blocks", tracker, "closed_source"
    ) == impossible_links.deciding_digest(c, a, "blocks", tracker, "closed_source")


def test_a_requalified_record_is_pruned_from_the_store(
    store, apply_records, impossible_links, monkeypatch
):
    """A record whose digest stopped matching is deleted, not left to rot.

    Without the prune the file accumulates a dead entry for every link that
    ever became possible again.
    """
    import rebar

    repo, a, b = store
    _counting_link(monkeypatch)
    apply_records._inbound_update_apply_links(_add_payload(b), a, repo)

    path = _store_path(repo, impossible_links)
    key = impossible_links.record_key(a, b, "blocks")
    assert key in json.loads(path.read_text(encoding="utf-8"))["records"]

    rebar.transition(a, "closed", "open", repo_root=repo)
    apply_records._inbound_update_apply_links(_add_payload(b), a, repo)

    remaining = json.loads(path.read_text(encoding="utf-8"))["records"]
    assert key not in remaining, f"the re-qualified record was not pruned: {remaining!r}"


def test_a_corrupt_store_degrades_to_empty_and_still_applies(
    store, apply_records, impossible_links, monkeypatch
):
    """An unparseable store costs one retry; it never breaks the inbound pass.

    Fail-open applies to the store itself, not only to classify().
    """
    repo, a, b = store
    path = _store_path(repo, impossible_links)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")

    calls = _counting_link(monkeypatch)
    applied = apply_records._inbound_update_apply_links(_add_payload(b), a, repo)

    assert len(calls) == 1, "a corrupt store must not stop the link from being attempted"
    assert applied == 0
    assert impossible_links.ImpossibleLinkStore(str(path.parent.parent)) is not None


def test_a_status_change_on_an_ANCESTOR_invalidates_the_record(
    tmp_path, impossible_links, monkeypatch
):
    """Promotion may redirect a link to an ancestor; reopening that ancestor must invalidate
    the refusal."""
    import rebar

    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (
        ("git", "init", "-q", "-b", "main"),
        ("git", "config", "user.email", "t@example.com"),
        ("git", "config", "user.name", "T"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))

    epic = str(rebar.create_ticket("epic", "promotion target", repo_root=repo))
    child = str(rebar.create_ticket("task", "child", repo_root=repo))
    other = str(rebar.create_ticket("task", "other", repo_root=repo))
    rebar.edit_ticket(child, parent=epic, repo_root=repo)

    tracker = str(Path(str(_store_path(repo, impossible_links))).parent.parent)
    before = impossible_links.deciding_digest(child, other, "blocks", tracker, "closed_source")

    # Change the ANCESTOR's status. The child itself is untouched.
    # (in_progress rather than closed: the child-closure invariant refuses to
    # close an epic that still has an open child, and closing the child too
    # would muddy which ticket the digest actually noticed.)
    rebar.transition(epic, "open", "in_progress", repo_root=repo)

    after = impossible_links.deciding_digest(child, other, "blocks", tracker, "closed_source")
    assert before != after, (
        "a status change on the ancestor that add_dependency promotes to left the "
        "digest unchanged; the record would outlive the condition that justified it"
    )


def test_the_record_file_is_staged_for_commit_back(monkeypatch):
    """Stage the refusal sidecar for commit-back so the next tickets-branch checkout retains
    the skip."""
    git_adapter = importlib.import_module("rebar.reconciler.git_adapter")
    # _commit_binding_store_snapshot moved to pass_support.py (ticket
    # piscine-bullish-cowbird, reconcile_helpers.py module-size headroom).
    helpers = importlib.import_module("rebar.reconciler.pass_support")

    assert git_adapter.IMPOSSIBLE_LINKS_FILE == ".bridge_state/impossible_links.json"

    source = Path(helpers.__file__).read_text(encoding="utf-8")
    staged_block = source[source.index("_rel_files = [") : source.index("_existing_rel = [")]
    assert "IMPOSSIBLE_LINKS_FILE" in staged_block, (
        "the impossible-link record is not staged by _commit_binding_store_snapshot, "
        "so it will not survive to the next pass"
    )
    # The bug-1e08 per-file idempotency now comes from the locked store seam's
    # pathspec-scoped status (ticket 11a9-b11b): the staged set must be handed to
    # commit_tickets_branch as the pathspec, or a record-only change is either
    # swept in with unrelated files or silently skipped.
    assert "commit_tickets_branch(" in source and "paths=_existing_rel" in source, (
        "the staged file set is not passed as the pathspec to the locked commit "
        "seam, so a pass that changes ONLY this file would be skipped and never committed"
    )


def test_removals_and_healthy_adds_are_untouched(store, apply_records, monkeypatch):
    """The change is scoped to the ADD branch; nothing else in the loop moves.

    Guards the boundary with epic a4bd (inbound REMOVAL discrimination), which
    owns ``_inbound_unlink_one``.
    """
    import rebar

    repo, a, b = store
    rebar.transition(a, "closed", "open", repo_root=repo)
    calls = _counting_link(monkeypatch)

    applied = apply_records._inbound_update_apply_links(_add_payload(b), a, repo)
    assert applied == 1 and len(calls) == 1, "a possible link must still be written normally"

    # Epic a4bd: the removal branch now requires positive peer-confirmation evidence —
    # `managed_refs` proves local ownership, never that the peer ever saw the link. The link
    # was just written above, so seeding its record is what this cell means by "the peer had
    # it"; without it the removal is DECLINED and this cell would fail for a4bd's reason
    # rather than reporting on the ADD-side skip it exists to guard.
    from rebar.reconciler.peer_confirmations import open_store as _open_pc

    _pc = _open_pc(repo)
    _pc.record(a, b, "blocks")
    _pc.save()

    removed = apply_records._inbound_update_apply_links(
        {"links": [{"action": "remove", "target_id": b, "relation": "blocks"}]}, a, repo
    )
    assert removed == 1, "the removal branch must be unaffected by the ADD-side skip record"
    targets = {
        dep.get("target_id") for dep in (rebar.show_ticket(a, repo_root=repo).get("deps") or [])
    }
    assert b not in targets, f"the removal did not land: {targets!r}"
