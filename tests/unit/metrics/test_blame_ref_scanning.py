"""Commit-message ref scanning in the post-close `caused_by` derivation.

Regression cover for bug ``c50e-7326-9cac-45e4`` (postwar-bardic-walleye): closing a bug ran
``derive_caused_by`` over the WHOLE branch history and, for every commit, re-resolved the bug
id and every candidate that ``extract_ticket_refs`` harvested from the commit subject. Those
candidates are not user-supplied, so unrelated ambiguity was printed as an error; and because
any non-full/non-short candidate falls through to ``rebar._ids._scan_alias`` (which opens up
to two JSON files PER ticket directory), the scan cost O(commits x tickets) and appeared to
hang after the close had already committed.

These tests pin the two properties that fix it — SILENCE and BOUNDED WORK — plus the
behaviour-preservation guarantee that no id form gained or lost the ability to resolve.
"""

from __future__ import annotations

import json

import pytest

from rebar._alias import compute_alias
from rebar._ids import resolve_ticket_id
from rebar.metrics import blame

_TARGET = "a9dd-7326-9cac-45e4"
_SIBLING = "a9dd-1111-2222-3333"  # shares the first quad -> `a9dd` is ambiguous


def _mk_tracker(tmp_path, tickets: dict[str, str]) -> str:
    """A minimal on-disk tracker: one directory per ticket, each with a CREATE event
    carrying the stored alias. That is exactly what the resolver's alias scan reads."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    for ticket_id, alias in tickets.items():
        d = tracker / ticket_id
        d.mkdir()
        (d / "0001-CREATE.json").write_text(json.dumps({"data": {"alias": alias}}))
    return str(tracker)


def _log(*subjects: str) -> str:
    """A `git log --format=%H%x1f%B%x1e` payload with one record per subject."""
    return "".join(f"sha{i}\x1f{s}\n\x1e" for i, s in enumerate(subjects))


@pytest.fixture
def tracker(tmp_path) -> str:
    return _mk_tracker(
        tmp_path,
        {_TARGET: "postwar-bardic-walleye", _SIBLING: "other-sample-ticket"},
    )


# ── silence ───────────────────────────────────────────────────────────────────
def test_ambiguous_subject_candidate_is_not_reported_as_an_error(
    tracker, monkeypatch, capsys
) -> None:
    """`a9dd` IS a prefix of the target, so it genuinely reaches the resolver and is
    genuinely ambiguous — the resolution still fails, but silently."""
    monkeypatch.setattr(blame, "_git", lambda *args: _log("a9dd: an unrelated old commit"))

    assert blame._find_fixing_commit("/repo", _TARGET, tracker) is None
    assert "Ambiguous prefix" not in capsys.readouterr().err


def test_culprit_commit_subject_is_also_resolved_quietly(tracker, monkeypatch, capsys) -> None:
    """The second scanner: `_commit_ticket` reads the culprit commit's own subject."""
    monkeypatch.setattr(blame, "_git", lambda *args: "a9dd: the culprit commit\n")

    assert blame._commit_ticket("/repo", "sha0", tracker) is None
    assert "Ambiguous prefix" not in capsys.readouterr().err


# ── bounded work ──────────────────────────────────────────────────────────────
def _counting_resolver(monkeypatch) -> list[str]:
    """Record every ref handed to the real resolver, keeping real behaviour."""
    seen: list[str] = []
    real = blame.resolve_ticket_id

    def spy(ticket_id, tracker, quiet=False):
        seen.append(ticket_id)
        return real(ticket_id, tracker, quiet=quiet)

    monkeypatch.setattr(blame, "resolve_ticket_id", spy)
    return seen


@pytest.mark.parametrize("commits", [10, 200])
def test_distinct_unrelated_subjects_cost_no_store_lookups(tracker, monkeypatch, commits) -> None:
    """The original hang: N commits with DISTINCT 4-hex subjects meant N alias scans.

    None of these fragments is a prefix of the target, so none of them can resolve to it —
    the short-circuit proves that without touching the store. Only the loop-invariant bug-id
    resolution remains, and its cost does not grow with the history length.
    """
    subjects = [f"{i:04x}: unrelated commit {i}" for i in range(1, commits + 1)]
    monkeypatch.setattr(blame, "_git", lambda *args: _log(*subjects))
    seen = _counting_resolver(monkeypatch)

    assert blame._find_fixing_commit("/repo", _TARGET, tracker) is None
    assert seen == [_TARGET], "work must not scale with the number of commits"


def test_a_recurring_candidate_is_resolved_once_not_once_per_commit(tracker, monkeypatch) -> None:
    """Memoization: the same ambiguous candidate across 50 commits costs one lookup."""
    monkeypatch.setattr(blame, "_git", lambda *args: _log(*["a9dd: same ref"] * 50))
    seen = _counting_resolver(monkeypatch)

    assert blame._find_fixing_commit("/repo", _TARGET, tracker) is None
    assert seen == [_TARGET, "a9dd"]


# ── behaviour preservation ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "subject",
    [
        f"{_TARGET}: cited by full canonical id",
        "a9dd-7326: cited by 8-digit two-quad prefix",
        "postwar-bardic-walleye: cited by alias",
    ],
    ids=["full-id", "two-quad", "alias"],
)
def test_supported_reference_forms_still_identify_the_fixing_commit(
    tracker, monkeypatch, subject
) -> None:
    monkeypatch.setattr(blame, "_git", lambda *args: _log("unrelated: nothing", subject))

    assert blame._find_fixing_commit("/repo", _TARGET, tracker) == "sha1"


def test_an_unambiguous_bare_four_hex_reference_still_resolves(tmp_path, monkeypatch) -> None:
    """4-digit references are deprecated, NOT broken: where the quad is unique it still works."""
    lone = _mk_tracker(tmp_path, {_TARGET: "postwar-bardic-walleye"})
    monkeypatch.setattr(blame, "_git", lambda *args: _log("a9dd: cited by bare 4-hex"))

    assert blame._find_fixing_commit("/repo", _TARGET, lone) == "sha0"


def test_trailer_references_are_unaffected(tracker, monkeypatch) -> None:
    """The `rebar-ticket:` trailer is the identity-bearing form and must keep working."""
    monkeypatch.setattr(
        blame, "_git", lambda *args: _log(f"a subject with no id\n\nrebar-ticket: {_TARGET}")
    )

    assert blame._find_fixing_commit("/repo", _TARGET, tracker) == "sha0"


# ── the short-circuit's soundness ─────────────────────────────────────────────
def test_a_hex_shaped_ref_that_is_not_a_prefix_of_the_target_is_skipped() -> None:
    assert blame._cannot_resolve_to("b100", _TARGET, "postwar-bardic-walleye") is True
    assert blame._cannot_resolve_to("a9dd", _TARGET, "postwar-bardic-walleye") is False


def test_three_quad_forms_are_never_skipped() -> None:
    """3-quad forms DO reach the alias scan (a wordlist alias is adj-noun-noun), so they are
    excluded from the short-circuit and always take the real resolver."""
    assert blame._is_prefix_only_form("dead-beef-cafe") is False
    assert blame._cannot_resolve_to("dead-beef-cafe", _TARGET, "postwar-bardic-walleye") is False


def test_alias_shaped_refs_are_never_skipped() -> None:
    assert blame._cannot_resolve_to("postwar-bardic-walleye", _TARGET, "") is False
    assert blame._cannot_resolve_to("REB-310", _TARGET, "") is False


def test_the_targets_own_alias_is_never_skipped_even_if_hex_shaped(tmp_path) -> None:
    """`dead` and `deed` are both real wordlist entries, so `dead-deed` is a GENERATABLE
    alias that also has hex-quad shape. The guard must not skip the target's own alias."""
    assert blame._is_prefix_only_form("dead-deed") is True
    assert blame._cannot_resolve_to("dead-deed", _TARGET, "dead-deed") is False


def test_skipping_a_hex_shaped_alias_of_another_ticket_matches_the_real_resolver(
    tmp_path,
) -> None:
    """Soundness of the 2-quad skip, pinned against the resolver itself rather than against a
    claim about the wordlist: a 2-quad ref takes ``_SHORT_ID_RE``'s branch, which returns
    BEFORE the alias scan. So even a real, hex-shaped stored alias does not resolve by that
    name, and skipping it changes no outcome.
    """
    tracker = _mk_tracker(tmp_path, {_TARGET: "postwar-bardic-walleye", _SIBLING: "dead-deed"})

    assert resolve_ticket_id("dead-deed", tracker) is None  # the invariant being relied on
    assert blame._cannot_resolve_to("dead-deed", _TARGET, "postwar-bardic-walleye") is True


# ── the target's effective alias ──────────────────────────────────────────────
def test_effective_alias_prefers_the_stored_alias(tracker, monkeypatch) -> None:
    """A stored alias wins over the computed one — the resolver's own precedence, which
    matters wherever the wordlist has drifted since the ticket was created."""
    monkeypatch.setattr(blame, "reduce_ticket", lambda _p: {"alias": "stored-drifted-alias"})

    assert blame._effective_alias(_TARGET, tracker) == "stored-drifted-alias"


def test_effective_alias_falls_back_to_the_computed_alias(tracker, monkeypatch) -> None:
    """No stored alias -> the computed one, matching the resolver's alias-scan precedence."""
    monkeypatch.setattr(blame, "reduce_ticket", lambda _p: {})

    assert blame._effective_alias(_TARGET, tracker) == compute_alias(_TARGET)


def test_effective_alias_of_an_absent_ticket_is_still_the_computed_alias(tmp_path) -> None:
    """A missing directory is not fatal: derivation continues with the computable alias."""
    empty = _mk_tracker(tmp_path, {})

    assert blame._effective_alias(_TARGET, empty) == compute_alias(_TARGET)


def test_effective_alias_survives_an_unreadable_ticket(tracker, monkeypatch) -> None:
    def boom(_path):
        raise OSError("unreadable")

    monkeypatch.setattr(blame, "reduce_ticket", boom)

    assert blame._effective_alias(_TARGET, tracker) == compute_alias(_TARGET)


# ── shape classification boundaries ───────────────────────────────────────────
@pytest.mark.parametrize(
    "ref",
    ["a9dd", "a9dd-7326", "a9dd-7326-9cac-45e4"],
    ids=["one-quad", "two-quad", "four-quad"],
)
def test_prefix_only_shapes(ref) -> None:
    assert blame._is_prefix_only_form(ref) is True


@pytest.mark.parametrize(
    "ref",
    ["a9dd-7326-9cac", "a9dd-7326-9cac-45e4-0000", "a9d", "a9ddd", "A9DD", "a9zz", "", "REB-310"],
    ids=[
        "three-quad",
        "five-quad",
        "too-short",
        "too-long",
        "uppercase",
        "non-hex",
        "empty",
        "jira",
    ],
)
def test_non_prefix_only_shapes(ref) -> None:
    assert blame._is_prefix_only_form(ref) is False


def test_an_exact_target_match_short_circuits_before_the_store(tracker, monkeypatch) -> None:
    """A commit citing the canonical id needs no lookup at all."""
    monkeypatch.setattr(blame, "_git", lambda *args: _log(f"{_TARGET}: the fix"))
    seen = _counting_resolver(monkeypatch)

    assert blame._find_fixing_commit("/repo", _TARGET, tracker) == "sha0"
    assert seen == [_TARGET]
