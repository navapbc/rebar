"""Module-size seam for the inbound differ (story 64ae-262f-990a-49ae).

``inbound_differ.py`` sat at EXACTLY the 800-line hard cap, so any edit to it failed
the CI module-size gate. The file is split along the seam its own call graph already
had: the COLLECTION-VALUED diffs — the helpers that fill ``InboundMutation.comments``
/ ``.labels`` / ``.links`` — move to the sibling ``inbound_collection_diffs`` leaf,
leaving ``inbound_differ`` with the scalar-field diff and the pass orchestration.

The seam is real, not a line-count carve:

* every one of the four collection helpers is called from exactly ONE place,
  ``compute_inbound_mutations``, and from nowhere else in the package;
* they call nothing in the module except each other's shared private helpers
  (``_load_link_direction``, ``RECONCILER_MARKER``, ``_EXCLUDED_PREFIXES``), which are
  used by no other symbol, so the extracted set is closed;
* they share one signature shape — ``(jira_fields, local_ticket, ...) ->
  list[dict]`` mutation records — against the scalar half's ``-> dict`` of fields.

What this file pins:

1. The four collection diffs (and their closed helper set) are DEFINED in
   ``inbound_collection_diffs``, not in ``inbound_differ``.
2. ``inbound_differ`` still exposes every moved name, bound to the SAME object — the
   back-compat re-export that keeps ``inbound_differ.<symbol>`` attribute access and
   the by-path module loads used across this test tree working unchanged.
3. Both modules stay inside the size band the module-size policy sets: at least 100
   lines (no sliver files) and at least 100 lines of REAL headroom under the cap read
   from ``.github/module-size-limit.txt`` — landing back at the cap is the trap this
   story exists to clear.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from module_size_support import read_limit

pytestmark = pytest.mark.unit

_ENGINE = Path(__file__).resolve().parents[4] / "src" / "rebar" / "_engine"
_REC = _ENGINE / "rebar_reconciler"

# The closed set extracted across the seam: the four collection-valued diffs plus the
# private helpers that only they use.
_MOVED_FUNCTIONS = (
    "_diff_comments_inbound",
    "_diff_labels_inbound",
    "_diff_links_inbound",
    "_diff_link_removals_inbound",
    "_load_link_direction",
)
_MOVED_CONSTANTS = ("RECONCILER_MARKER", "_EXCLUDED_PREFIXES")

_MIN_LOC = 100
_MIN_HEADROOM = 100


@pytest.mark.parametrize("name", _MOVED_FUNCTIONS)
def test_collection_diffs_are_defined_in_the_extracted_module(name: str) -> None:
    """Each moved helper's DEFINING module is the collection-diff leaf."""
    from rebar_reconciler import inbound_collection_diffs

    fn = getattr(inbound_collection_diffs, name)
    assert fn.__module__.endswith("inbound_collection_diffs"), (
        f"{name} must be DEFINED in inbound_collection_diffs, not re-exported into it; "
        f"found __module__={fn.__module__!r}"
    )


@pytest.mark.parametrize("name", _MOVED_FUNCTIONS + _MOVED_CONSTANTS)
def test_inbound_differ_still_exposes_every_moved_name(name: str) -> None:
    """Back-compat: ``inbound_differ.<symbol>`` resolves to the very same object.

    Tests across this tree load ``inbound_differ.py`` by path and reach for these
    names as attributes; the re-export is what keeps those callers unmodified.
    """
    from rebar_reconciler import inbound_collection_diffs, inbound_differ

    assert hasattr(inbound_differ, name), (
        f"inbound_differ must keep re-exporting {name} after the split"
    )
    assert getattr(inbound_differ, name) is getattr(inbound_collection_diffs, name)


@pytest.mark.parametrize("filename", ("inbound_differ.py", "inbound_collection_diffs.py"))
def test_both_sides_of_the_seam_sit_inside_the_size_band(filename: str) -> None:
    """No sliver file, and real headroom under the cap — measured the way CI measures."""
    cap = read_limit()
    loc = (_REC / filename).read_text(encoding="utf-8").count("\n")
    assert loc >= _MIN_LOC, (
        f"{filename} is {loc} lines — a split must not produce a sliver module (minimum {_MIN_LOC})"
    )
    assert loc <= cap - _MIN_HEADROOM, (
        f"{filename} is {loc} lines against a {cap}-line cap — the split must leave at "
        f"least {_MIN_HEADROOM} lines of headroom, not land back at the ceiling"
    )
