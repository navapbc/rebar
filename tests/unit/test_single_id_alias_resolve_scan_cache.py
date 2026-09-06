"""Resolving ONE ticket id must not re-parse the whole store on every call.

`resolve_ticket_id` reaches `_scan_alias` for the alias form (and for the
deprecated 4-digit fragment, which tries the alias branch before falling through
to prefix resolution). `_scan_alias` reads and JSON-parses every ticket's
CREATE / SNAPSHOT event to compute that ticket's effective alias, so K single-id
resolutions against an UNCHANGED store cost K full-store passes. On a store of a
few thousand tickets that is thousands of JSON parses per resolution, and a
single `show_ticket` resolves twice — which is why a long-lived process (the MCP
server) spends the bulk of a single-ticket read scanning the store.

The contract asserted here is a bounded number of ticket-event reads across
repeated resolutions, plus unchanged resolution semantics for every accepted id
form, unchanged ambiguity handling, and invalidation whenever a ticket's stored
alias could have changed.
"""

from __future__ import annotations

import builtins
import json
import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import config
from rebar._ids import resolve_ticket_id

pytestmark = pytest.mark.unit

_TICKETS = 12


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real store with several tickets; yields ``(repo, tracker_dir, created)``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    created = [
        rebar.create_ticket("task", f"ticket {i}", repo_root=str(repo), return_alias=True)
        for i in range(_TICKETS)
    ]
    return repo, str(config.tracker_dir(str(repo))), created


class _EventReadCounter:
    """Counts opens of ticket EVENT files under the tracker.

    That is the observable per-resolution parse cost a full-store alias scan
    pays: one read + one `json.load` per ticket, every call.
    """

    def __init__(self, tracker: str) -> None:
        self._tracker = os.path.normpath(tracker)
        self._real_open = builtins.open
        self.count = 0

    def _counting_open(self, file, *args, **kwargs):
        try:
            path = os.fspath(file)
        except TypeError:
            path = ""
        if isinstance(path, str) and path.endswith(".json"):
            if os.path.normpath(path).startswith(self._tracker + os.sep):
                self.count += 1
        return self._real_open(file, *args, **kwargs)

    def __enter__(self) -> _EventReadCounter:
        builtins.open = self._counting_open
        return self

    def __exit__(self, *exc: object) -> bool:
        builtins.open = self._real_open
        return False


def test_repeated_alias_resolution_does_not_rescan_the_whole_store(store) -> None:
    _repo, tracker, created = store
    alias, canonical = created[0]["alias"], created[0]["id"]

    # Let the first resolution pay whatever one-pass work it legitimately needs.
    assert resolve_ticket_id(alias, tracker) == canonical

    with _EventReadCounter(tracker) as counter:
        for _ in range(5):
            assert resolve_ticket_id(alias, tracker) == canonical

    assert counter.count < _TICKETS, (
        f"{counter.count} ticket-event reads for 5 repeat resolutions of ONE alias in a "
        f"{_TICKETS}-ticket store — single-id resolution is still scanning the whole store"
    )


def test_a_warm_single_id_resolution_parses_no_ticket_events(store) -> None:
    """The steady-state cost of a warm resolve must stay parse-free.

    Bounds the residual per-call cost of whatever revalidation replaces the scan:
    a re-validation that reintroduced a per-ticket read would regress toward the
    original full-store parse and this pins that it does not.
    """
    _repo, tracker, created = store
    alias, canonical = created[3]["alias"], created[3]["id"]
    assert resolve_ticket_id(alias, tracker) == canonical

    with _EventReadCounter(tracker) as counter:
        assert resolve_ticket_id(alias, tracker) == canonical
    assert counter.count == 0, (
        f"a warm single-id resolve read {counter.count} ticket event files; it must read none"
    )


def test_resolution_semantics_hold_for_every_accepted_id_form(store) -> None:
    _repo, tracker, created = store
    canonical, alias = created[0]["id"], created[0]["alias"]

    # Warm the resolver first, then re-assert every form against that warm state.
    resolve_ticket_id(alias, tracker)

    assert resolve_ticket_id(canonical, tracker) == canonical, "full canonical id"
    assert resolve_ticket_id(canonical[:9], tracker) == canonical, "8-digit two-quad short id"
    assert resolve_ticket_id(alias, tracker) == canonical, "alias"
    # Deprecated 4-digit single-quad fragment: still resolves when unambiguous.
    assert resolve_ticket_id(canonical[:4], tracker) == canonical, "4-digit fragment"
    # Unknown tokens resolve to nothing, warm or cold.
    assert resolve_ticket_id("no-such-alias-here", tracker) is None
    assert resolve_ticket_id("ffff-ffff", tracker) is None


def test_jira_key_resolves_through_the_binding_store(store) -> None:
    _repo, tracker, created = store
    canonical = created[1]["id"]
    bridge = Path(tracker) / ".bridge_state"
    bridge.mkdir(exist_ok=True)
    (bridge / "bindings.json").write_text(
        json.dumps({"reverse": {"REB-4242": canonical}}), encoding="utf-8"
    )
    resolve_ticket_id(created[0]["alias"], tracker)  # warm
    assert resolve_ticket_id("REB-4242", tracker) == canonical


def test_a_ticket_created_after_a_warm_resolution_is_still_resolvable(store) -> None:
    repo, tracker, created = store
    assert resolve_ticket_id(created[0]["alias"], tracker) == created[0]["id"]

    fresh = rebar.create_ticket("task", "created later", repo_root=str(repo), return_alias=True)
    assert resolve_ticket_id(fresh["alias"], tracker) == fresh["id"], (
        "a ticket created after a warm resolution must resolve by alias"
    )
    assert resolve_ticket_id(created[0]["alias"], tracker) == created[0]["id"]


def test_an_alias_change_inside_an_existing_ticket_dir_is_picked_up(store) -> None:
    """Invalidation must fire for a change WITHIN a ticket dir, not only for
    ticket creation.

    Mirrors compaction: the CREATE event is retired and a SNAPSHOT carrying the
    compiled state becomes the alias source. A resolver that only noticed new
    ticket directories would keep serving the pre-change alias.
    """
    _repo, tracker, created = store
    canonical = created[4]["id"]
    old_alias = created[4]["alias"]
    assert resolve_ticket_id(old_alias, tracker) == canonical

    ticket_dir = Path(tracker) / canonical
    for event in ticket_dir.glob("*-CREATE.json"):
        event.rename(event.with_suffix(".json.retired"))
    snapshot = ticket_dir / "9000000000000000000-aaaaaaaa-1111-4111-8111-111111111111-SNAPSHOT.json"
    snapshot.write_text(
        json.dumps({"data": {"compiled_state": {"alias": "renamed-after-the-fact"}}}),
        encoding="utf-8",
    )

    assert resolve_ticket_id("renamed-after-the-fact", tracker) == canonical
    assert resolve_ticket_id(old_alias, tracker) is None


def test_an_ambiguous_alias_still_refuses_to_resolve(store, capsys) -> None:
    _repo, tracker, created = store
    victim = created[2]
    # Hand-build a second ticket dir whose CREATE carries the SAME alias.
    twin = Path(tracker) / "aaaa-bbbb-cccc-4ddd"
    twin.mkdir()
    (twin / "1000000000000000000-11111111-1111-4111-8111-111111111111-CREATE.json").write_text(
        json.dumps({"data": {"alias": victim["alias"]}}), encoding="utf-8"
    )
    assert resolve_ticket_id(victim["alias"], tracker) is None
    assert "Ambiguous alias" in capsys.readouterr().err
