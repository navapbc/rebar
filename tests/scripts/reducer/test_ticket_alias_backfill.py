"""Tests for rebar._alias.compute_alias and the read-time backfill
applied by ticket_reducer._processors.process_create.

Tier E E7d: the bash-era helpers ``ticket-alias-compute.py`` (alias computation)
and ``ticket-alias-resolve.py`` (alias/jira_key resolution) were thin CLI wrappers
over the in-process logic — ``rebar._alias.compute_alias`` and
``rebar._engine_support.resolver.resolve_ticket_id`` respectively. These tests
exercise that in-process logic directly instead of subprocessing the (deleted)
helpers.

Behaviours under test:
  - compute_alias returns adj-noun-noun for full 16-hex IDs
  - compute_alias returns adj-noun (2 words) for legacy 8-hex IDs
  - process_create populates state['alias'] from data.alias when present
  - process_create backfills state['alias'] from ticket_id when data.alias missing
  - resolve_ticket_id resolves by stored/backfilled alias, skips dotfile dirs and
    malformed CREATE events, and fails loud (returns None + stderr diagnostic) on
    an unreadable tracker directory
  - resolve_ticket_id resolves a Jira issue key (REB-NNN) via the binding store
    reverse index, degrading to None (never raising) when the store is
    missing/corrupt or the binding is stale; the dead data.jira_key scan is gone
"""

import json
import time
import uuid
from pathlib import Path

from rebar._alias import compute_alias, compute_genesis_alias
from rebar._engine_support.resolver import (
    _resolve_via_binding_store,
    _scan_alias,
    resolve_ticket_id,
)
from rebar.reducer import reduce_ticket

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


# ── Edge-case coverage for resolve_ticket_id alias resolution ─────────────────
#
# Tier E E7d: ticket-alias-resolve.py was a thin CLI over
# rebar._engine_support.resolver. In-process, resolve_ticket_id returns the
# resolved ticket-dir name (or None on no-match / hard failure) and prints
# diagnostics to stderr; _scan_alias returns the alias matches (or None on a hard
# tracker-listing failure). We assert on those return values + captured stderr
# (via capsys) instead of subprocess streams.


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


def _write_bindings(tracker: Path, reverse: dict) -> None:
    """Plant a binding-store reverse index at <tracker>/.bridge_state/bindings.json."""
    bs = tracker / ".bridge_state"
    bs.mkdir(parents=True, exist_ok=True)
    (bs / "bindings.json").write_text(json.dumps({"version": 1, "reverse": reverse}))


# ── Jira-key resolution via the binding store ─────────────────────────────────
#
# The authoritative Jira↔rebar mapping is the reconciler's binding store reverse
# index (.bridge_state/bindings.json), NOT a data.jira_key field on CREATE events
# (that field is never written — the old scan for it was dead code, now removed).
# resolve_ticket_id resolves a Jira-key-shaped input via _resolve_via_binding_store.


def test_resolver_jira_key_resolves_via_binding_store(tmp_path):
    """A Jira key bound in the binding store resolves to its local ticket dir."""
    td = _plant_ticket(tmp_path, "abcd-efab-1234-5678", alias_in_data="some-alias")
    _write_bindings(tmp_path, {"REB-310": td.name})
    assert _resolve_via_binding_store("REB-310", str(tmp_path)) == td.name
    assert resolve_ticket_id("REB-310", str(tmp_path)) == td.name


def test_resolver_jira_key_lowercase_input_uppercased(tmp_path):
    """Jira project keys are canonically upper-case; a lower-case input resolves
    via the upper-cased fallback lookup."""
    td = _plant_ticket(tmp_path, "abcd-efab-1234-5678", alias_in_data="some-alias")
    _write_bindings(tmp_path, {"REB-310": td.name})
    assert resolve_ticket_id("reb-310", str(tmp_path)) == td.name


def test_resolver_unbound_jira_key_returns_none(tmp_path):
    """A Jira-shaped input with no binding resolves to None (no false alias/prefix
    match)."""
    _plant_ticket(tmp_path, "abcd-efab-1234-5678", alias_in_data="some-alias")
    _write_bindings(tmp_path, {"REB-1": "0000-0000-0000-0000"})
    assert resolve_ticket_id("REB-999", str(tmp_path)) is None


def test_resolver_jira_key_missing_bindings_file_returns_none(tmp_path):
    """No binding store at all → Jira resolution unavailable → None, never raises."""
    _plant_ticket(tmp_path, "abcd-efab-1234-5678", alias_in_data="some-alias")
    assert _resolve_via_binding_store("REB-310", str(tmp_path)) is None
    assert resolve_ticket_id("REB-310", str(tmp_path)) is None


def test_resolver_jira_key_corrupt_bindings_file_returns_none(tmp_path):
    """A corrupt/garbage bindings.json must degrade to None, not raise."""
    _plant_ticket(tmp_path, "abcd-efab-1234-5678", alias_in_data="some-alias")
    bs = tmp_path / ".bridge_state"
    bs.mkdir()
    (bs / "bindings.json").write_text("{ not valid json")
    assert _resolve_via_binding_store("REB-310", str(tmp_path)) is None
    assert resolve_ticket_id("REB-310", str(tmp_path)) is None


def test_resolver_jira_key_binding_to_missing_dir_returns_none(tmp_path):
    """A binding that points at a ticket dir that no longer exists resolves to
    None — the mapping is stale, not a resolvable ticket."""
    _write_bindings(tmp_path, {"REB-310": "dead-beef-dead-beef"})
    assert _resolve_via_binding_store("REB-310", str(tmp_path)) is None
    assert resolve_ticket_id("REB-310", str(tmp_path)) is None


def test_scan_alias_no_longer_buckets_jira_keys(tmp_path):
    """Regression: _scan_alias is alias-only. A data.jira_key on a CREATE event is
    NOT a resolution source any more (only data.alias / computed alias is)."""
    td = tmp_path / "abcd-efab-1234-5678"
    td.mkdir()
    ts = time.time_ns()
    (td / f"{ts}-x-CREATE.json").write_text(
        json.dumps({"data": {"alias": "real-alias", "jira_key": "REB-777"}})
    )
    # The alias still resolves; the jira_key field does not.
    assert _scan_alias("real-alias", str(tmp_path)) == [td.name]
    assert _scan_alias("REB-777", str(tmp_path)) == []


def test_resolver_nonzero_exit_propagates_to_resolve_ticket_id(tmp_path, capsys):
    """When the tracker dir is unreadable, resolve_ticket_id must surface the
    failure (return None AND emit a stderr diagnostic) rather than report a
    silent zero-match — a silent zero-match looks identical to 'ticket not found'
    and hides operational problems.

    The former bash-CLI variant of this test (sourcing ticket-lib.sh and shelling
    out to the alias resolver) checked the same intent across the deleted
    subprocess boundary; in-process the diagnostic is _scan_alias's
    "cannot list" stderr line, surfaced by resolve_ticket_id."""
    # A tracker path that is a FILE not a dir → os.listdir raises OSError →
    # _scan_alias prints "cannot list ..." and returns None → resolve
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
    from rebar import _alias as alias_mod

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


# ── v2 genesis alias (adjective-adjective-animal) ─────────────────────────────
#
# New tickets use compute_genesis_alias (gfycat adjective-adjective-animal),
# persisted onto the CREATE event at create time. Legacy tickets are unaffected:
# their read-time backfill still uses compute_alias (adjective-noun-noun). These
# tests pin the new format and prove the two paths are independent.


def test_genesis_alias_is_adj_adj_animal():
    """A full 16-hex id yields a 3-word alias drawn from the v2 wordlist: the first
    two words are adjectives, the third an animal, and the adjectives differ."""
    from rebar._alias import _load_v2

    adjs, animals = _load_v2()
    adj_set, animal_set = set(adjs), set(animals)
    alias = compute_genesis_alias("0193-d61d-abcd-1234")
    assert alias is not None
    a1, a2, animal = alias.split("-")
    assert a1 in adj_set and a2 in adj_set, f"first two words must be adjectives: {alias!r}"
    assert animal in animal_set, f"third word must be an animal: {alias!r}"
    assert a1 != a2, f"the two adjectives must differ: {alias!r}"


def test_genesis_alias_deterministic_and_too_short_is_none():
    tid = "0193-d61d-abcd-1234"
    assert compute_genesis_alias(tid) == compute_genesis_alias(tid)
    # Native ids are always 16-hex; a <12-hex id can't fill three slots → None.
    assert compute_genesis_alias("0193-d61d") is None
    assert compute_genesis_alias("abc") is None


def test_genesis_and_legacy_paths_are_independent():
    """The create path (genesis, adj-adj-animal) and the legacy backfill path
    (compute_alias, adj-noun-noun) must produce different aliases for the same id —
    proving the format switch applies only to new tickets and leaves the legacy
    read-time backfill untouched."""
    tid = "9894-a463-090a-43e5"
    # Legacy value is pinned by other tests in this file (== 'real-soil-anger').
    assert compute_genesis_alias(tid) != compute_alias(tid)
    # Legacy stays 3 words but from the noun list; genesis is 3 words from v2.
    assert len(compute_genesis_alias(tid).split("-")) == 3


def test_load_v2_warns_once_when_wordlist_missing(tmp_path, capsys, monkeypatch):
    """When the v2 wordlist is unavailable, _load_v2() emits a one-shot stderr
    diagnostic and callers fall back to a hex alias — mirroring the legacy loader."""
    from rebar import _alias as alias_mod

    monkeypatch.setattr(alias_mod, "_WORDS_V2_CACHE", None)
    monkeypatch.setattr(alias_mod, "_WARNED_MISSING_V2", False)
    monkeypatch.setattr(
        alias_mod, "_wordlist_v2_path", lambda: str(tmp_path / "does-not-exist.txt")
    )
    alias_mod._load_v2()
    alias_mod._load_v2()
    # With the wordlist gone, genesis falls back to the hex prefix (no crash).
    assert alias_mod.compute_genesis_alias("0193-d61d-abcd-1234") == "0193d61d"
    occurrences = capsys.readouterr().err.count("ticket-wordlist-v2.txt unavailable")
    assert occurrences == 1, f"expected exactly one WARN, saw {occurrences}"
