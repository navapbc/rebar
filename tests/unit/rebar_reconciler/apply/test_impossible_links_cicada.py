"""Bug b8b1: structurally-impossible inbound links are attempted once, not every pass.

The defect: ``_inbound_update_apply_links`` called ``rebar.link`` for every
Jira-sourced ADD record, caught the failure, logged a WARNING and forgot it.
Three of those failures are deterministic verdicts about the LOCAL graph — the
source is closed, the endpoints are already in an ancestor-descendant
relationship, the edge would close a cycle — so the next pass re-derived the
identical record and re-spent the write. Measured on four consecutive live
Reconcile Bridge passes: 19 doomed writes each, a byte-identical set every time.

The oracle for the headline cells below is **the number of ``rebar.link``
invocations across two passes**, not the log. That distinction is the whole
point of the ticket: suppressing the WARNING would satisfy a log-shaped
assertion while leaving the waste entirely intact. So ``rebar.link`` is wrapped
in a counter that still calls THROUGH to the real facade against a real store —
the failures here are genuine ``add_dependency`` verdicts, not simulated ones.
"""

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
    """A real rebar store with two tickets, the source CLOSED.

    Closing the source is what makes ``rebar.link(a, b, ...)`` raise the real
    "source ticket '<id>' is closed" verdict — the most common of the three
    shapes in the live logs (5 of 19).
    """
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
    return importlib.import_module("rebar_reconciler.apply_inbound_records")


@pytest.fixture
def impossible_links():
    return importlib.import_module("rebar_reconciler.impossible_links")


def _counting_link(monkeypatch) -> list[tuple]:
    """Wrap ``rebar.link`` in a call counter that still calls through."""
    import rebar

    real_link = rebar.link
    calls: list[tuple] = []

    def counting(src, dst, relation, *, repo_root=None):
        calls.append((src, dst, relation))
        return real_link(src, dst, relation, repo_root=repo_root)

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
    """An unrecognised failure returns None so the caller keeps retrying it.

    The last case is the trap worth naming: a message containing the words "is
    closed" but not the "cannot create" verdict marker must NOT be filed as a
    permanent structural impossibility.
    """
    assert impossible_links.classify(text) is None


# ---------------------------------------------------------------------------
# The headline oracle: write attempts across two passes
# ---------------------------------------------------------------------------


def test_impossible_link_is_attempted_once_then_never_again(store, apply_records, monkeypatch):
    """THE ACCEPTANCE ORACLE — pass 1 attempts the write, pass 2 attempts nothing.

    Counting ``rebar.link`` invocations (not log lines) is what separates a real
    fix from log suppression: this cell fails if the WARNING is merely silenced.
    """
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
    """The verdict lands in .bridge_state/impossible_links.json and reads back.

    Durability is what makes the skip visible to an operator rather than a
    silent in-process swallow, and what makes it survive the process boundary
    between two reconcile passes (each pass is a separate process).
    """
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
    """When the deciding input changes, the record stops matching and we retry.

    This is the self-healing property: the digest keys on the endpoints'
    status/ancestry/deps, so reopening the closed source re-qualifies the link
    without anyone clearing the store by hand. And this time the write lands.
    """
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
    """Non-structural edits must NOT invalidate the record.

    If the digest keyed on the whole ticket, every comment would send the
    reconciler back to re-attempting — the churn would return through the back
    door on an active store.
    """
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
    """A failure that is not provably structural keeps the old retry behaviour.

    The dangerous failure mode of this change is over-classification: a
    transient fault filed as permanent would silently stop syncing a legitimate
    link. Two passes must produce two attempts and an empty store.
    """
    import rebar

    repo, a, b = store
    calls: list[tuple] = []

    def flaky(src, dst, relation, *, repo_root=None):
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
    """A genuinely new impossible link is loud once; the steady state is quiet.

    Both halves matter. Losing the first WARNING would make the skip a silent
    swallow; keeping it on every pass would leave the permanent error floor the
    ticket was filed about.
    """
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

    removed = apply_records._inbound_update_apply_links(
        {"links": [{"action": "remove", "target_id": b, "relation": "blocks"}]}, a, repo
    )
    assert removed == 1, "the removal branch must be unaffected by the ADD-side skip record"
    targets = {
        dep.get("target_id") for dep in (rebar.show_ticket(a, repo_root=repo).get("deps") or [])
    }
    assert b not in targets, f"the removal did not land: {targets!r}"
