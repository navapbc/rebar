"""The inbound-apply leaf helpers are split along their concern boundary, and stay split.

``apply_inbound_records.py`` sat at 799 LOC against the locked 800-LOC cap, so any edit to it
— even adding a comment — failed the CI module-size gate. It was split along the seam its own
call graph and import graph already drew:

* **Event-file materialisation** — the ``_inbound_create_*`` / ``_inbound_update_*`` phase
  helpers, every one of which terminates in ``inbound_translate._write_event_file`` — now lives
  in ``apply_inbound_events.py``.
* **rebar-facade mutation** — the inbound assignee identity mint and the inbound link-graph
  cluster, which write through ``rebar.ensure_identity_for`` / ``rebar.link`` / ``rebar.unlink``
  and the impossible-link and peer-confirmation sidecar stores, and write no event files at all
  — stays in ``apply_inbound_records.py``.

These assertions are the regression oracle for BOTH halves of that split: the boundary (a
module that writes event files must not also mutate the graph through the facade, and vice
versa) and the headroom the split bought. Both fail against the pre-split single module, so
they discriminate — the seam cannot silently re-merge, and neither half can drift back into the
gate's near-cap warning band without CI saying so.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

# Bare-name import: ``tests/conftest.py`` puts the ``tests/`` directory on sys.path, so the
# module-size rule has ONE definition shared with ``tests/unit/test_module_size_contract.py``
# rather than a second copy of the limit here.
from module_size_support import REPO_ROOT, read_limit

_REC = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"
_RECORDS = _REC / "apply_inbound_records.py"
_EVENTS = _REC / "apply_inbound_events.py"

#: The floor the module-size policy sets for a file created by a split (docs/architecture.md:
#: "never create files < 100 LOC by splitting").
_MIN_LOC = 100

#: Calls that mark a module as an EVENT-FILE WRITER.
_EVENT_WRITE_CALLS = ("_write_event_file(",)

#: Calls that mark a module as a REBAR-FACADE MUTATOR of the shared graph/registry.
_FACADE_MUTATION_CALLS = ("rebar.link(", "rebar.unlink(", "ensure_identity_for(")

#: Every phase helper the orchestrator in ``apply_inbound.py`` drives, plus the loop-breaker
#: marker it re-exports. The split must not change what that module resolves.
_ORCHESTRATOR_SURFACE = (
    "_RECONCILER_MARKER_APPLIER",
    "_inbound_create_write_create_event",
    "_inbound_create_write_status_event",
    "_inbound_create_record_binding",
    "_inbound_create_writeback_jira",
    "_inbound_update_write_edit_event",
    "_inbound_update_write_status_event",
    "_inbound_update_apply_labels",
    "_inbound_update_apply_comments",
    "_inbound_update_apply_links",
)


def _loc(path: Path) -> int:
    """``wc -l`` semantics — newline count, exactly what the CI module-size gate measures."""
    return path.read_text(encoding="utf-8").count("\n")


def _near_cap_band() -> int:
    """The gate's own leading-indicator band: ``LIMIT - LIMIT // 10``.

    A file above it is reported by CI as "within 10% of the cap; split it before it breaches
    the hard cap". Landing either half of a split back in that band would recreate the problem
    the split exists to solve, so it is asserted, not merely warned about."""
    limit = read_limit()
    return limit - limit // 10


@pytest.mark.parametrize("path", [_RECORDS, _EVENTS], ids=["records", "events"])
def test_each_half_is_within_the_split_size_policy(path: Path) -> None:
    """Neither half is a thin shim, and neither sits in the gate's near-cap warning band."""
    assert path.is_file(), f"{path.name} is missing — the inbound-apply split is incomplete"
    loc = _loc(path)
    band = _near_cap_band()
    assert _MIN_LOC <= loc <= band, (
        f"{path.name} is {loc} LOC; a file created or left by a split must be at least "
        f"{_MIN_LOC} LOC (no thin shims) and at most {band} LOC (the module-size gate's "
        f"near-cap band, LIMIT - LIMIT // 10), so it keeps real headroom under the cap"
    )


def test_the_records_half_writes_no_event_files() -> None:
    """The facade-mutation half must not reacquire the event-writing concern."""
    text = _RECORDS.read_text(encoding="utf-8")
    offenders = [call for call in _EVENT_WRITE_CALLS if call in text]
    assert not offenders, (
        f"apply_inbound_records.py calls {offenders} — event-file materialisation belongs in "
        f"apply_inbound_events.py. This module owns the rebar-facade mutations (identity mint "
        f"and the inbound link graph) and writes no events."
    )


def test_the_events_half_makes_no_facade_mutation() -> None:
    """The event-writing half must not reacquire the graph/registry-mutation concern."""
    text = _EVENTS.read_text(encoding="utf-8")
    offenders = [call for call in _FACADE_MUTATION_CALLS if call in text]
    assert not offenders, (
        f"apply_inbound_events.py calls {offenders} — mutations through the rebar facade "
        f"(the identity mint, rebar.link / rebar.unlink) belong in apply_inbound_records.py. "
        f"This module only materialises Jira records as local reducer event files."
    )


def test_the_orchestrator_surface_survives_the_split() -> None:
    """``apply_inbound`` still resolves every phase helper it drove before the split.

    The split moved helpers between modules; the orchestrator's contract is unchanged, so a
    move that forgot to repoint an import fails here rather than at reconcile time."""
    apply_inbound = importlib.import_module("rebar_reconciler.apply_inbound")
    missing = [name for name in _ORCHESTRATOR_SURFACE if not hasattr(apply_inbound, name)]
    assert not missing, (
        f"rebar_reconciler.apply_inbound no longer resolves {missing} — repoint its imports at "
        f"the module that now owns each helper"
    )
