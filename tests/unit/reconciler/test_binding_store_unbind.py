"""BindingStore.unbind must clear both indexes independently (bug 874a).

A missing jira_key or forward entry must not strand a reverse key. The public
unbind path must remove reverse_missing_forward residue without disturbing
other bindings."""

from __future__ import annotations

import importlib
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[3] / "src" / "rebar" / "reconciler"


BindingStore = importlib.import_module("rebar.reconciler.binding_store").BindingStore


def _store(tmp_path: Path) -> BindingStore:
    return BindingStore(tmp_path / ".tickets-tracker")


def test_unbind_clears_reverse_when_forward_entry_lost_its_jira_key(tmp_path):
    """Unbind a reverse key even when its forward binding has lost jira_key."""
    store = _store(tmp_path)
    store.bind_confirm("local-1", "REB-410")
    # Strip the forward jira_key, leaving reverse[REB-410] -> local-1 intact.
    # This models any out-of-band route that rewrites bindings.json (a prune, a
    # manual edit, or a merge=ours artifact on the tickets branch).
    del store._data["bindings"]["local-1"]["jira_key"]

    store.unbind("local-1")

    assert "REB-410" not in store._data["reverse"], (
        "unbind() stranded the reverse key because the forward entry carried no "
        "jira_key — the reverse index must be cleared on its own authority"
    )
    assert "local-1" not in store._data["bindings"]


def test_unbind_clears_reverse_when_forward_entry_is_already_gone(tmp_path):
    """A reverse key orphaned by an out-of-band forward removal is still clearable.

    This is what makes the prune verb expressible through the public API: for an
    already-orphaned key the forward pop is a no-op and the reverse sweep does
    the work.
    """
    store = _store(tmp_path)
    store.bind_confirm("local-2", "REB-411")
    del store._data["bindings"]["local-2"]

    store.unbind("local-2")

    assert "REB-411" not in store._data["reverse"]


def test_unbind_clears_both_indexes_for_a_confirmed_binding(tmp_path):
    """Regression: the ordinary path keeps working (the O(1) keyed pop)."""
    store = _store(tmp_path)
    store.bind_confirm("local-3", "REB-412")
    assert store._data["reverse"]["REB-412"] == "local-3"

    store.unbind("local-3")

    assert "local-3" not in store._data["bindings"]
    assert "REB-412" not in store._data["reverse"]


def test_unbind_leaves_other_bindings_untouched(tmp_path):
    """The reverse sweep must be scoped to the unbound local_id only."""
    store = _store(tmp_path)
    store.bind_confirm("local-4", "REB-413")
    store.bind_confirm("local-5", "REB-414")

    store.unbind("local-4")

    assert store._data["bindings"]["local-5"]["jira_key"] == "REB-414"
    assert store._data["reverse"] == {"REB-414": "local-5"}


def test_unbind_of_an_unknown_local_id_is_a_no_op(tmp_path):
    """An unknown id must not raise and must not disturb the indexes."""
    store = _store(tmp_path)
    store.bind_confirm("local-6", "REB-415")

    store.unbind("never-bound")

    assert store._data["bindings"]["local-6"]["jira_key"] == "REB-415"
    assert store._data["reverse"] == {"REB-415": "local-6"}
