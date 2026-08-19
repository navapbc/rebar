"""Unit tests for BindingStore — local-id ↔ jira-key binding persistence.

Follows the importlib loader convention established across this test tree
(see conftest.py docstring for rationale).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# importlib loader — convention per conftest.py
# ---------------------------------------------------------------------------
_SRC = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "rebar"
    / "_engine"
    / "rebar_reconciler"
    / "binding_store.py"
)

_spec = importlib.util.spec_from_file_location("binding_store", _SRC)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

BindingStore = _mod.BindingStore
load_binding_store = _mod.load_binding_store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> BindingStore:
    """Fresh BindingStore backed by a temporary directory."""
    return BindingStore(tmp_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBindingLifecycle:
    """bind_pending → bind_confirm full lifecycle."""

    def test_bind_pending_then_confirm(self, store: BindingStore) -> None:
        store.bind_pending("abc-1234")
        assert store.is_bound("abc-1234")
        assert store.is_pending("abc-1234")
        assert store.get_jira_key("abc-1234") is None

        store.bind_confirm("abc-1234", "DIG-42")
        assert store.is_bound("abc-1234")
        assert not store.is_pending("abc-1234")
        assert store.get_jira_key("abc-1234") == "DIG-42"


class TestQueries:
    def test_get_jira_key_returns_none_for_unbound(self, store: BindingStore) -> None:
        assert store.get_jira_key("nonexistent") is None

    def test_reverse_lookup(self, store: BindingStore) -> None:
        store.bind_pending("local-1")
        store.bind_confirm("local-1", "DIG-99")
        assert store.get_local_id("DIG-99") == "local-1"

    def test_reverse_lookup_returns_none_for_unknown_key(self, store: BindingStore) -> None:
        assert store.get_local_id("DIG-0") is None

    def test_pending_bindings_listed(self, store: BindingStore) -> None:
        store.bind_pending("a")
        store.bind_pending("b")
        store.bind_confirm("b", "DIG-1")
        assert store.pending_bindings() == ["a"]

    def test_confirmed_count(self, store: BindingStore) -> None:
        store.bind_pending("x")
        store.bind_confirm("x", "DIG-10")
        store.bind_pending("y")
        assert store.confirmed_count() == 1


class TestUnbind:
    def test_unbind_removes_both_directions(self, store: BindingStore) -> None:
        store.bind_pending("tid")
        store.bind_confirm("tid", "DIG-7")
        assert store.is_bound("tid")
        assert store.get_local_id("DIG-7") == "tid"

        store.unbind("tid")
        assert not store.is_bound("tid")
        assert store.get_jira_key("tid") is None
        assert store.get_local_id("DIG-7") is None

    def test_unbind_noop_for_unknown(self, store: BindingStore) -> None:
        store.unbind("ghost")  # should not raise


class TestPersistence:
    def test_save_and_reload(self, tmp_path: Path) -> None:
        store1 = BindingStore(tmp_path)
        store1.bind_pending("id-a")
        store1.bind_confirm("id-a", "DIG-50")
        store1.save()

        store2 = BindingStore(tmp_path)
        assert store2.get_jira_key("id-a") == "DIG-50"
        assert store2.get_local_id("DIG-50") == "id-a"
        assert not store2.is_pending("id-a")

    def test_atomic_save(self, tmp_path: Path) -> None:
        """Verify the save path uses tempfile + os.replace (not direct write).

        We confirm atomicity by checking that if the store directory
        already exists, save() produces a file (not a partial write),
        and no temp files are left behind.
        """
        store = BindingStore(tmp_path)
        store.bind_pending("t1")
        store.save()

        bridge_dir = tmp_path / ".bridge_state"
        # Both durable stores are complete, with no atomic-write temp left behind.
        files = {path.name for path in bridge_dir.iterdir()}
        assert files == {"bindings.json", "get_rotation.json"}
        assert not any(name.endswith(".tmp") for name in files)

        # Verify content is valid JSON
        with open(bridge_dir / "bindings.json") as f:
            data = json.load(f)
        with open(bridge_dir / "get_rotation.json") as f:
            rotation = json.load(f)
        # Version 2 adds the ADR 0026 per-binding baseline (epic 3006-e198); a
        # version-1 store without baselines still reads (back-compat).
        assert data["version"] == 2
        assert "t1" in data["bindings"]
        assert rotation == {"version": 1, "last_get_pass": {}}


class TestRecovery:
    def test_recover_pending_found_in_jira(self, store: BindingStore) -> None:
        """Recovery with a mock that returns a hit for any query (legacy behavior).

        Updated to accept colon-form as the primary search label — the
        assert_called_once_with check is replaced by a call_args inspection
        that confirms the FIRST call uses the colon form.
        """
        store.bind_pending("lost-1")

        client = MagicMock()
        client.search_issues.return_value = [{"key": "DIG-200"}]

        count = store.recover_pending_bindings(client)

        assert count == 1
        assert store.get_jira_key("lost-1") == "DIG-200"
        assert not store.is_pending("lost-1")
        # The FIRST search must use the canonical colon form.
        first_call_arg = client.search_issues.call_args_list[0][0][0]
        assert first_call_arg == 'labels = "rebar-id:lost-1"', (
            f"Primary search must use colon form; got: {first_call_arg!r}"
        )

    def test_recover_pending_not_found_in_jira(self, store: BindingStore) -> None:
        """A create that genuinely never landed is unbound — but only once absence is
        CORROBORATED.

        This test previously asserted the unbind after a SINGLE negative search. That
        assertion encoded bug 21fc: on Jira DC the keyless-pending state is entered exactly
        when we crashed during create_issue, and the Lucene index is eventually consistent
        (JRASERVER-70423: a 2,991s lag observed), so one empty search is precisely what a
        LIVE issue looks like — and unbinding on it made the next pass write a DUPLICATE
        Jira issue.

        The test's intent is unchanged and still asserted: a truly-absent issue must not
        strand its ticket pending forever. What changed is that absence must now be
        corroborated by repeated misses AND an entry older than the index-lag grace window.
        The complementary guard — that a SINGLE miss does NOT unbind — lives in
        ``test_index_lag_duplicate_heldout.py``.
        """
        store.bind_pending("orphan-1")
        # Age the entry past the index-lag grace window; without this the misses alone
        # prove nothing, which is the whole point of the fix.
        store._data["bindings"]["orphan-1"]["created_at"] = "2000-01-01T00:00:00Z"

        client = MagicMock()
        client.search_issues.return_value = []

        counts = [store.recover_pending_bindings(client) for _ in range(3)]

        assert counts[-1] == 1, f"the corroborated unbind never resolved: {counts}"
        assert not store.is_bound("orphan-1")

    def test_recover_with_no_pending_is_noop(self, store: BindingStore) -> None:
        client = MagicMock()
        assert store.recover_pending_bindings(client) == 0
        client.search_issues.assert_not_called()

    # ------------------------------------------------------------------
    # NEW tests — bug 8a1f-fd52-a416-4776 regression tests
    # ------------------------------------------------------------------

    def test_recover_colon_form_primary_hit(self, store: BindingStore) -> None:
        """Client returns a result ONLY for colon-form JQL — binding confirmed.

        This is the RED test: before the fix, the code searches hyphen-form
        (rebar-id-{id}) which returns no results, so the binding is discarded.
        After the fix, colon-form (rebar-id:{id}) is the primary search and
        matches the mock, confirming the binding to DIG-999.
        """
        store.bind_pending("abc-5678")

        def selective_search(jql: str):
            # Only return a hit for the canonical colon-form label.
            if jql == 'labels = "rebar-id:abc-5678"':
                return [{"key": "DIG-999"}]
            return []

        client = MagicMock()
        client.search_issues.side_effect = selective_search

        count = store.recover_pending_bindings(client)

        assert count == 1, "recover_pending_bindings must count the entry"
        assert store.get_jira_key("abc-5678") == "DIG-999", (
            "Binding must be confirmed from colon-form search"
        )
        assert not store.is_pending("abc-5678"), (
            "Entry must no longer be pending after colon-form recovery"
        )

    def test_recover_hyphen_form_legacy_fallback(self, store: BindingStore) -> None:
        """Client returns a result ONLY for hyphen-form JQL — legacy fallback.

        Old issues written before the colon→hyphen migration may carry a
        rebar-id-{id} label.  The recovery logic must attempt the hyphen-form
        when the colon-form search returns nothing.
        """
        store.bind_pending("xyz-0001")

        def selective_search(jql: str):
            # Only return a hit for the legacy hyphen-form label.
            if jql == 'labels = "rebar-id-xyz-0001"':
                return [{"key": "DIG-100"}]
            return []

        client = MagicMock()
        client.search_issues.side_effect = selective_search

        count = store.recover_pending_bindings(client)

        assert count == 1
        assert store.get_jira_key("xyz-0001") == "DIG-100", (
            "Binding must be confirmed from hyphen-form legacy fallback"
        )
        assert not store.is_pending("xyz-0001")

    def test_recover_colon_form_wins_when_both_present(self, store: BindingStore) -> None:
        """When both colon-form and hyphen-form would match, colon-form is used.

        The colon search must be attempted first; because it returns a hit,
        the hyphen-form fallback must NOT be called.
        """
        store.bind_pending("dup-0042")

        client = MagicMock()
        client.search_issues.return_value = [{"key": "DIG-42"}]

        store.recover_pending_bindings(client)

        assert store.get_jira_key("dup-0042") == "DIG-42"
        # Only ONE search_issues call: colon form found the issue immediately.
        assert client.search_issues.call_count == 1, (
            "Should stop at colon-form hit; hyphen fallback must not be called"
        )
        first_call_arg = client.search_issues.call_args_list[0][0][0]
        assert first_call_arg == 'labels = "rebar-id:dup-0042"'


class TestLoadBindingStore:
    def test_load_binding_store_creates_instance(self, tmp_path: Path) -> None:
        tracker = tmp_path / ".tickets-tracker"
        tracker.mkdir()
        repo_root = tmp_path
        bs = load_binding_store(repo_root)
        assert isinstance(bs, BindingStore)
        assert not bs.is_bound("anything")


class TestRebindStaleReverse:
    def test_bind_confirm_rebind_drops_stale_reverse_key(self, store: BindingStore) -> None:
        """c244: rebinding a local_id to a NEW jira_key must drop the OLD key's reverse
        entry in the same save, else reverse[old_key] dangles at the local_id forever
        (e.g. after a Jira hard-delete -> outbound re-create rebinds to a fresh key)."""
        local_id = "reb-rebind-1"
        store.bind_confirm(local_id, "OLD-1")
        assert store.get_local_id("OLD-1") == local_id
        assert store.get_jira_key(local_id) == "OLD-1"

        # Rebind to a new key (the hard-delete re-create path).
        store.bind_confirm(local_id, "NEW-2")

        assert store.get_jira_key(local_id) == "NEW-2"
        assert store.get_local_id("NEW-2") == local_id
        assert store.get_local_id("OLD-1") is None, "stale reverse[OLD-1] must be dropped"

    def test_bind_confirm_same_key_is_idempotent(self, store: BindingStore) -> None:
        """Re-confirming the SAME key must not remove its own reverse entry."""
        local_id = "reb-rebind-2"
        store.bind_confirm(local_id, "SAME-9")
        store.bind_confirm(local_id, "SAME-9")
        assert store.get_local_id("SAME-9") == local_id
        assert store.get_jira_key(local_id) == "SAME-9"


# ---------------------------------------------------------------------------
# RP-02 S1 T2 (evadable-curious-mastodon) — repository delegation.
#
# T2 routes the facade's persistence through ``BindingRepository`` without
# changing its mature caller contract. These are CHARACTERIZATION tests: they
# are green on the pre-delegation facade by design (that is the point — the
# contract must not move), so their teeth come from defect-seeded mutation of
# the delegating code, not from a RED-first run. The differential assertions
# below compare the facade against the repository directly, so they keep
# meaning after the cutover rather than freezing stale golden bytes.
# ---------------------------------------------------------------------------

import inspect  # noqa: E402

from rebar_reconciler import get_rotation as _get_rotation  # noqa: E402
from rebar_reconciler import peer_state as _peer_state  # noqa: E402
from rebar_reconciler.binding_repository import BindingRepository  # noqa: E402

_BRIDGE = Path(".bridge_state")


def _state_bytes(tracker: Path) -> dict[str, bytes]:
    """Every binding-state file that exists, as raw bytes."""
    bridge = tracker / _BRIDGE
    if not bridge.is_dir():
        return {}
    return {p.name: p.read_bytes() for p in sorted(bridge.iterdir()) if p.suffix == ".json"}


class TestRepositoryDelegation:
    """The facade stays authoritative and byte-compatible over the repository."""

    def test_public_signatures_unchanged(self, tmp_path: Path) -> None:
        """AC1. These are the mature caller contract — the reconciler, the adapters and
        `bridge fsck` all bind to them, so delegation must not reshape a single one."""
        expected = {
            "__init__": "(self, tracker_dir: 'Path') -> 'None'",
            "get_jira_key": "(self, local_id: 'str') -> 'str | None'",
            "get_local_id": "(self, jira_key: 'str') -> 'str | None'",
            "is_bound": "(self, local_id: 'str') -> 'bool'",
            "is_pending": "(self, local_id: 'str') -> 'bool'",
            "all_bindings": "(self) -> 'dict[str, dict]'",
            "pending_bindings": "(self) -> 'list[str]'",
            "confirmed_count": "(self) -> 'int'",
            "bind_pending": "(self, local_id: 'str') -> 'None'",
            "record_pending_key": "(self, local_id: 'str', jira_key: 'str') -> 'None'",
            "bind_confirm": "(self, local_id: 'str', jira_key: 'str') -> 'None'",
            "unbind": "(self, local_id: 'str') -> 'None'",
            "save": "(self) -> 'None'",
            "is_retired": "(self, jira_key: 'str') -> 'bool'",
            "unretire": "(self, jira_key: 'str') -> 'bool'",
            "clear_absent": "(self, jira_key: 'str') -> 'None'",
            "note_absent": "(self, jira_key: 'str') -> 'None'",
            "record_comment_id": (
                "(self, local_comment_key: 'str', jira_comment_id: 'str') -> 'None'"
            ),
            "comment_id_for": "(self, local_comment_key: 'str') -> 'str | None'",
            "retired_key_for_local": "(self, local_id: 'str') -> 'str | None'",
        }
        for name, signature in expected.items():
            assert hasattr(BindingStore, name), f"public method {name} disappeared"
            assert str(inspect.signature(getattr(BindingStore, name))) == signature, name

    def test_all_bindings_stays_a_shallow_outer_copy(self, store: BindingStore) -> None:
        """AC2. The copy depth is load-bearing and must NOT change here: the outer
        mapping is a fresh dict (dropping a key does not unbind anything), while the
        inner entries are the SAME objects, which is what `peer_state` overlays and the
        rich-text handler have always relied on. S2 adds a narrow named operation as the
        supported mutation seam; this slice keeps the shallow contract intact."""
        store.bind_confirm("loc-A", "DIG-A")
        snapshot = store.all_bindings()

        assert snapshot is not store.all_bindings()
        del snapshot["loc-A"]
        assert store.get_jira_key("loc-A") == "DIG-A"

        store.all_bindings()["loc-A"]["probe"] = 1
        assert store.all_bindings()["loc-A"]["probe"] == 1

    def test_pending_confirmed_and_reverse_survive_a_reload(self, tmp_path: Path) -> None:
        """AC2. The state machine's observable answers must be identical after a real
        round trip through the files the repository now owns."""
        first = BindingStore(tmp_path)
        first.bind_pending("loc-P")
        first.record_pending_key("loc-K", "DIG-K")
        first.bind_confirm("loc-C", "DIG-C")
        first.save()

        second = BindingStore(tmp_path)

        assert second.is_pending("loc-P") is True
        assert second.get_jira_key("loc-P") is None
        assert second.is_pending("loc-K") is True
        assert second.get_jira_key("loc-K") == "DIG-K"
        assert second.is_pending("loc-C") is False
        assert second.get_jira_key("loc-C") == "DIG-C"
        assert second.get_local_id("DIG-C") == "loc-C"
        assert second.confirmed_count() == 1
        assert sorted(second.pending_bindings()) == ["loc-K", "loc-P"]

    def test_facade_writes_byte_identical_state_to_the_repository(self, tmp_path: Path) -> None:
        """AC3. Differential oracle: the same logical state persisted through the facade
        and through the repository must produce byte-identical files. This is what makes
        the delegation safe to land — the committed bytes do not move."""
        via_facade = tmp_path / "facade"
        via_repo = tmp_path / "repo"
        (via_facade / _BRIDGE).mkdir(parents=True)
        (via_repo / _BRIDGE).mkdir(parents=True)

        store = BindingStore(via_facade)
        store.bind_confirm("loc-A", "DIG-A")
        store.save()

        repo = BindingRepository(via_repo)
        repo.bindings["loc-A"] = dict(store.all_bindings()["loc-A"])
        repo.reverse["DIG-A"] = "loc-A"
        repo.save()

        assert _state_bytes(via_facade) == _state_bytes(via_repo)
        assert set(_state_bytes(via_facade)) == {"bindings.json", "get_rotation.json"}

    def test_facade_and_repository_agree_on_the_corruption_disposition(
        self, tmp_path: Path
    ) -> None:
        """AC3. The exception SURFACE is part of the contract, not just the bytes: live
        corruption fails closed through both entry points with the same error, and the
        corrupt file survives for the operator either way."""
        bridge = tmp_path / _BRIDGE
        bridge.mkdir(parents=True)
        raw = '{"bindings": {<<<<<<< HEAD\n'
        (bridge / "bindings.json").write_text(raw, encoding="utf-8")

        with pytest.raises(ValueError, match="corrupt or contains git conflict"):
            BindingStore(tmp_path)
        with pytest.raises(ValueError, match="corrupt or contains git conflict"):
            BindingRepository(tmp_path)

        assert (bridge / "bindings.json").read_text(encoding="utf-8") == raw

    def test_peer_state_and_rotation_remain_independent_owners(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC4. `peer_state.py` and `get_rotation.py` keep their own ownership; the
        repository must not absorb or re-implement them, or their dedicated suites stop
        being the oracle for that behaviour."""
        seen: list[str] = []
        real_set_baseline = _peer_state.set_baseline
        real_rotation_save = _get_rotation.save

        def spy_baseline(bindings: object, local_id: str, fields: object) -> object:
            seen.append("peer_state.set_baseline")
            return real_set_baseline(bindings, local_id, fields)  # type: ignore[arg-type]

        def spy_rotation(path: object, stamps: object) -> object:
            seen.append("get_rotation.save")
            return real_rotation_save(path, stamps)  # type: ignore[arg-type]

        monkeypatch.setattr(_peer_state, "set_baseline", spy_baseline)
        monkeypatch.setattr(_get_rotation, "save", spy_rotation)

        store = BindingStore(tmp_path)
        store.bind_confirm("loc-A", "DIG-A")
        store.set_baseline("loc-A", {"summary": "s"})
        store.set_last_get("DIG-A", "pass-1")
        store.save()

        assert seen == ["peer_state.set_baseline", "get_rotation.save"]
        assert store.get_baseline("loc-A") == {"summary": "s"}
        assert store.last_get_pass("DIG-A") == "pass-1"

    def test_no_public_attribute_exposes_a_writable_repository(self, tmp_path: Path) -> None:
        """AC5. The facade stays authoritative. If an adapter or reconciler caller could
        reach the repository it would be able to write binding state without passing
        through the facade's coordination — exactly the mutation-through-query hazard
        this epic is closing."""
        store = BindingStore(tmp_path)

        for name in dir(store):
            if name.startswith("_"):
                continue
            member = getattr(store, name, None)
            assert not isinstance(member, BindingRepository), (
                f"public attribute {name!r} hands out the repository"
            )
            if callable(member) and not inspect.signature(member).parameters:
                assert not isinstance(member(), BindingRepository), (
                    f"public method {name}() returns the repository"
                )

    def test_top_level_insert_on_a_legacy_store_reaches_the_persisted_bytes(
        self, tmp_path: Path
    ) -> None:
        """AC3 collateral invariant: the facade must ALIAS the repository's document, not
        hold its own copy of the outer mapping.

        A legacy store has no ``comment_ids`` key, so ``record_comment_id`` creates one
        with ``setdefault`` — a TOP-LEVEL insert. A shallow copy of the document still
        shares the inner ``bindings``/``reverse`` dicts, so it looks harmless and every
        other assertion here still passes; but a brand-new top-level key would land only
        in the facade's copy and never reach the bytes the repository serializes. Comment
        identity would be silently lost and every mirrored comment re-posted on the next
        pass (the DIG-5301 duplicate class). Found by mutation: this is the one delegation
        defect the rest of the suite cannot see.
        """
        bridge = tmp_path / _BRIDGE
        bridge.mkdir(parents=True)
        (bridge / "bindings.json").write_text(
            json.dumps({"version": 2, "bindings": {}, "reverse": {}}), encoding="utf-8"
        )

        store = BindingStore(tmp_path)
        store.record_comment_id("hlc-1", "10001")

        assert store.is_comment_mapped("hlc-1") is True
        persisted = json.loads((bridge / "bindings.json").read_text(encoding="utf-8"))
        assert persisted["comment_ids"] == {"hlc-1": "10001"}
        assert BindingStore(tmp_path).comment_id_for("hlc-1") == "10001"
