"""Tests for ticket_reducer._alias.compute_alias and the read-time backfill
applied by ticket_reducer._processors.process_create.

Tier E E7d: the bash-era helpers ``ticket-alias-compute.py`` (alias computation)
and ``ticket-alias-resolve.py`` (alias/jira_key resolution) were thin CLI wrappers
over the in-process logic — ``rebar.reducer._alias.compute_alias`` and
``rebar._engine_support.resolver.resolve_ticket_id`` respectively. These tests
exercise that in-process logic directly instead of subprocessing the (deleted)
helpers.

Behaviours under test:
  - compute_alias returns adj-noun-noun for full 16-hex IDs
  - compute_alias returns adj-noun (2 words) for legacy 8-hex IDs
  - process_create populates state['alias'] from data.alias when present
  - process_create backfills state['alias'] from ticket_id when data.alias missing
  - resolve_ticket_id resolves by stored/backfilled alias and jira_key, skips
    dotfile dirs and malformed CREATE events, and fails loud (returns None +
    stderr diagnostic) on an unreadable tracker directory
"""

import json
import time
import uuid
from pathlib import Path

from rebar._engine_support.resolver import _scan_alias_jira, resolve_ticket_id
from rebar.reducer import reduce_ticket
from rebar.reducer._alias import compute_alias

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_compute_alias_full_id_three_words():
    alias = compute_alias("0193-d61d-abcd-1234")
    assert alias is not None
    parts = alias.split("-")
    assert len(parts) == 3, f"expected adj-noun-noun, got {alias!r}"


def test_compute_alias_legacy_8hex_two_words():
    alias = compute_alias("0193-d61d")
    assert alias is not None
    parts = alias.split("-")
    assert len(parts) == 2, f"expected adj-noun, got {alias!r}"


def test_compute_alias_too_short_returns_none():
    assert compute_alias("abc") is None
    assert compute_alias("") is None


# NOTE (Tier E E7d): the former test_compute_alias_matches_shell_helper asserted
# byte-parity between compute_alias and the deleted ticket-alias-compute.py CLI.
# That helper was a thin re-export of compute_alias, so the parity check is now an
# identity (compute_alias == compute_alias) with no remaining in-process meaning;
# it has been dropped. compute_alias is exercised directly by the tests above and
# by the resolver backfill tests below.


def _plant_ticket(root: Path, ticket_id: str, alias_in_data: str | None) -> Path:
    """Write a minimal CREATE event for ticket_id, optionally with data.alias."""
    td = root / ticket_id
    td.mkdir(parents=True, exist_ok=True)
    ts = time.time_ns()
    ev = str(uuid.uuid4())
    data = {"ticket_type": "task", "title": "test"}
    if alias_in_data is not None:
        data["alias"] = alias_in_data
    payload = {
        "timestamp": ts,
        "uuid": ev,
        "event_type": "CREATE",
        "env_id": "",
        "author": "test",
        "data": data,
    }
    (td / f"{ts}-{ev}-CREATE.json").write_text(json.dumps(payload))
    return td


def test_process_create_prefers_stored_over_backfill(tmp_path):
    """When data.alias is present, process_create uses it verbatim — even when
    that stored value differs from what compute_alias(ticket_id) would yield.
    This guards against a regression where backfill clobbers the stored alias
    (a real risk if the conditional flips). The asserted value is intentionally
    NOT the deterministic backfill — a tautological "plant X, expect X" test
    would still pass if process_create returned a hardcoded value."""
    ticket_id = "aaaa-bbbb-cccc-dddd"
    backfill_would_yield = compute_alias(ticket_id)
    stored = "manually-chosen-alias"
    assert stored != backfill_would_yield, (
        "test invariant: stored must differ from backfill so the assertion "
        "actually proves stored-wins precedence"
    )
    td = _plant_ticket(tmp_path, ticket_id, stored)
    state = reduce_ticket(str(td))
    assert state["alias"] == stored


def test_process_create_backfills_when_alias_missing(tmp_path):
    """Legacy tickets (no data.alias on CREATE) should surface a computed alias."""
    td = _plant_ticket(tmp_path, "0193-d61d-abcd-1234", alias_in_data=None)
    state = reduce_ticket(str(td))
    expected = compute_alias("0193-d61d-abcd-1234")
    assert state["alias"] == expected
    assert state["alias"] is not None


# ── Edge-case coverage for resolve_ticket_id alias/jira_key resolution ────────
#
# Tier E E7d: ticket-alias-resolve.py was a thin CLI over
# rebar._engine_support.resolver. The CLI printed "alias\t<id>" / "jira\t<id>" on
# a match and exited non-zero on a hard tracker-listing failure. In-process,
# resolve_ticket_id returns the resolved ticket-dir name (or None on no-match /
# hard failure) and prints diagnostics to stderr; _scan_alias_jira exposes the
# jira-vs-alias bucketing the CLI's output prefix encoded. We assert on those
# return values + captured stderr (via capsys) instead of subprocess streams.


def test_resolver_missing_tracker_dir_fails_loud(tmp_path, capsys):
    """Non-existent tracker directory must fail loud — return None AND emit a
    stderr diagnostic, since silent emptiness is indistinguishable from
    'no matches found'."""
    result = resolve_ticket_id("anything", str(tmp_path / "does-not-exist"))
    assert result is None
    assert "cannot list" in capsys.readouterr().err


def test_resolver_malformed_create_json_is_skipped(tmp_path):
    """A ticket dir with corrupt CREATE event JSON must not crash the resolver —
    other tickets in the same tracker must still resolve."""
    # Valid ticket
    good = _plant_ticket(tmp_path, "1111-2222-3333-4444", "good-stored-alias")
    # Corrupt ticket
    bad = tmp_path / "5555-6666-7777-8888"
    bad.mkdir()
    ts = time.time_ns()
    (bad / f"{ts}-junk-CREATE.json").write_text("{ not valid json")
    assert resolve_ticket_id("good-stored-alias", str(tmp_path)) == good.name


def test_resolver_dotfile_dirs_ignored(tmp_path):
    """Entries starting with '.' (caches, lockfiles, the __bridge__ pseudo-dir
    by accident) must not be scanned — they aren't tickets."""
    (tmp_path / ".graph-cache").mkdir()
    (tmp_path / ".graph-cache" / "fake-CREATE.json").write_text(
        json.dumps({"data": {"alias": "should-not-match"}})
    )
    assert resolve_ticket_id("should-not-match", str(tmp_path)) is None


def test_resolver_backfilled_alias_matches_legacy_ticket(tmp_path):
    """A ticket with no data.alias must still resolve via the computed
    fallback — this is the whole point of the backfill."""
    td = _plant_ticket(tmp_path, "0193-d61d-abcd-1234", alias_in_data=None)
    expected = compute_alias("0193-d61d-abcd-1234")
    assert resolve_ticket_id(expected, str(tmp_path)) == td.name


def test_resolver_jira_key_takes_precedence_over_alias_collision(tmp_path):
    """If a single ticket's CREATE event has both a matching jira_key and a
    matching alias for the same input string (pathological), jira wins —
    matches the resolver's documented precedence order. _scan_alias_jira returns
    (jira_matches, alias_matches); the colliding input must land in jira_matches
    (the bucket the CLI's "jira\\t" prefix encoded), not alias_matches."""
    td = tmp_path / "abcd-efab-1234-5678"
    td.mkdir()
    ts = time.time_ns()
    (td / f"{ts}-x-CREATE.json").write_text(
        json.dumps(
            {
                "data": {"alias": "COLLIDE-99", "jira_key": "COLLIDE-99"},
            }
        )
    )
    jira_matches, alias_matches = _scan_alias_jira("COLLIDE-99", str(tmp_path))
    assert jira_matches == [td.name]
    assert alias_matches == []
    # And the public resolver still returns the ticket (jira precedence).
    assert resolve_ticket_id("COLLIDE-99", str(tmp_path)) == td.name


def test_resolver_nonzero_exit_propagates_to_resolve_ticket_id(tmp_path, capsys):
    """When the tracker dir is unreadable, resolve_ticket_id must surface the
    failure (return None AND emit a stderr diagnostic) rather than report a
    silent zero-match — a silent zero-match looks identical to 'ticket not found'
    and hides operational problems.

    The former bash-CLI variant of this test (sourcing ticket-lib.sh and shelling
    out to the alias resolver) checked the same intent across the deleted
    subprocess boundary; in-process the diagnostic is _scan_alias_jira's
    "cannot list" stderr line, surfaced by resolve_ticket_id."""
    # A tracker path that is a FILE not a dir → os.listdir raises OSError →
    # _scan_alias_jira prints "cannot list ..." and returns None → resolve
    # returns None.
    bad_tracker = tmp_path / ".tickets-tracker"
    bad_tracker.write_text("not a directory")

    result = resolve_ticket_id("some-alias", str(bad_tracker))
    err = capsys.readouterr().err
    assert result is None, "resolver failure must not resolve to a ticket"
    assert "cannot list" in err, f"expected explicit resolver diagnostic in stderr; got {err!r}"


def test_load_warns_once_when_wordlist_missing(tmp_path, capsys, monkeypatch):
    """When the wordlist is unavailable, _load() must emit a one-shot stderr
    diagnostic — silent fallback to the 8-hex alias hides a real
    misconfiguration. The warning must appear exactly once per process even
    across many _load() calls (cache + warned-flag both prevent re-emission)."""
    from rebar.reducer import _alias as alias_mod

    # Reset the module-level cache + warned flag so this test starts clean
    monkeypatch.setattr(alias_mod, "_WORDS_CACHE", None)
    monkeypatch.setattr(alias_mod, "_WARNED_MISSING", False)
    # Force the missing-wordlist fallback by pointing the (self-resolving) path
    # helper at a nonexistent file — TICKET_WORDLIST_PATH is no longer a knob.
    monkeypatch.setattr(alias_mod, "_wordlist_path", lambda: str(tmp_path / "does-not-exist.txt"))

    alias_mod._load()
    alias_mod._load()
    alias_mod._load()
    captured = capsys.readouterr()
    occurrences = captured.err.count("ticket-wordlist.txt unavailable")
    assert occurrences == 1, (
        f"expected exactly one WARN, saw {occurrences} in stderr: {captured.err!r}"
    )


# ── Bug 9894-a463-090a-43e5: SNAPSHOT-only ticket alias resolution ─────────────


def _plant_snapshot_ticket(
    root: Path, ticket_id: str, stored_alias: str, jira_key: str = ""
) -> Path:
    """Write a minimal SNAPSHOT event for ticket_id with the given compiled_state.alias.

    No CREATE event is written — this simulates a compacted ticket where only
    the SNAPSHOT remains.  The resolver must read compiled_state.alias from the
    SNAPSHOT instead of falling back to compute_alias(ticket_id).
    """
    td = root / ticket_id
    td.mkdir(parents=True, exist_ok=True)
    ts = time.time_ns()
    ev = str(uuid.uuid4())
    compiled_state: dict = {
        "ticket_id": ticket_id,
        "ticket_type": "task",
        "title": "compacted ticket",
        "alias": stored_alias,
        "status": "open",
    }
    if jira_key:
        compiled_state["jira_key"] = jira_key
    payload = {
        "timestamp": ts,
        "uuid": ev,
        "event_type": "SNAPSHOT",
        "env_id": "",
        "author": "compact-script",
        "data": {
            "compiled_state": compiled_state,
            "source_event_uuids": [],
        },
    }
    (td / f"{ts}-{ev}-SNAPSHOT.json").write_text(json.dumps(payload))
    return td


def test_resolver_snapshot_only_ticket_matches_stored_alias(tmp_path):
    """A SNAPSHOT-only ticket (no CREATE event) must resolve by the alias stored
    in compiled_state.alias, not by compute_alias(ticket_id).

    Bug 9894-a463-090a-43e5: the resolver fell back to compute_alias when no
    CREATE event was found, ignoring the authoritative stored alias in the
    SNAPSHOT's compiled_state.  After the fix the resolver must read the SNAPSHOT
    and emit the stored alias, not the computed one.

    Invariant: compute_alias('9894-a463-090a-43e5') == 'real-soil-anger'
               stored alias                          == 'brawny-gill-inlay'
               These must differ (verified below) so the test distinguishes
               stored-wins from compute_alias-wins.
    """
    ticket_id = "9894-a463-090a-43e5"
    stored = "brawny-gill-inlay"
    computed = compute_alias(ticket_id)

    # Invariant: the stored alias must differ from the computed alias so the test
    # actually distinguishes "resolver reads SNAPSHOT" from "resolver falls back
    # to compute_alias".
    assert stored != computed, (
        f"test invariant broken: stored alias {stored!r} must differ from "
        f"compute_alias({ticket_id!r}) == {computed!r}"
    )

    _plant_snapshot_ticket(tmp_path, ticket_id, stored_alias=stored)

    # Positive: resolver must find the ticket when given the stored alias.
    result = resolve_ticket_id(stored, str(tmp_path))
    assert result == ticket_id, (
        f"expected resolve to {ticket_id!r}; got {result!r}\n"
        f"(Bug 9894: resolver probably fell back to compute_alias={computed!r} "
        f"instead of reading compiled_state.alias={stored!r} from the SNAPSHOT)"
    )


def test_resolver_snapshot_only_ticket_does_not_match_computed_alias(tmp_path):
    """Negative control for bug 9894-a463-090a-43e5.

    When a SNAPSHOT-only ticket has a stored alias that differs from
    compute_alias(ticket_id), querying by the COMPUTED alias must NOT match —
    the stored alias in compiled_state is authoritative and the computed alias
    is irrelevant once overridden.

    If the resolver incorrectly falls back to compute_alias, this test passes
    with a match, which is wrong behavior (it contradicts the stored value).
    The correct post-fix behavior: compute_alias is not consulted for
    SNAPSHOT-only tickets; no match is found; stdout is empty.
    """
    ticket_id = "9894-a463-090a-43e5"
    stored = "brawny-gill-inlay"
    computed = compute_alias(ticket_id)

    # Same invariant guard as the positive test.
    assert stored != computed, (
        f"test invariant broken: stored alias {stored!r} must differ from "
        f"compute_alias({ticket_id!r}) == {computed!r}"
    )

    _plant_snapshot_ticket(tmp_path, ticket_id, stored_alias=stored)

    # Negative: querying by the computed alias must NOT match (stored wins).
    result = resolve_ticket_id(computed, str(tmp_path))
    assert result is None, (
        f"expected no match when querying by compute_alias={computed!r}; "
        f"got {result!r}\n"
        f"(Bug 9894: if a match appears here, the resolver incorrectly used "
        f"compute_alias instead of the SNAPSHOT compiled_state.alias={stored!r})"
    )
