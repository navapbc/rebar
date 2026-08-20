"""Module-size seam for the plan-review DET floor (story ecf9-5e55-7386-4f04).

``det_floor.py`` sat at 793 lines against the 800-line hard cap, so almost any edit to
it failed the CI module-size gate. It is split along the boundary its own docstring
already draws and its call graph already has: the checks that can BLOCK stay, and the
checks that can NEVER block move to the sibling ``det_advisory`` leaf.

The seam is real, not a line-count carve:

* the four never-blocking checks — ``p2_resolution``, ``p3_package_existence``,
  ``p6_ac_quality``, ``p7_destructive`` — have ZERO call edges, inbound or outbound,
  to any function that stays in ``det_floor``. P2/P3 reach only the grounding oracle,
  P6 only the already-extracted advisory-lint siblings, P7 only its own regexes;
* the retained checks are the connected ones: ``p1_readiness_shape`` and
  ``p4_oversize`` share ``_count_ac_items``, P1 adds ``_clarity_score``, P4 adds
  ``_description_limit``, ``p8_reviewability`` uses ``est_tokens``, and
  ``p5_task_dag`` uses ``det_lint``'s graph helpers;
* it repeats the extraction ``det_clarity`` (P10/P11) and ``det_lint`` (P9) already
  made from this module, re-exported the same way.

What this file pins:

1. The four never-blocking checks are DEFINED in ``det_advisory``, not in ``det_floor``.
2. ``det_floor`` still exposes every moved name, bound to the SAME object — the
   re-export that keeps ``det_floor.<check>`` attribute access and every
   ``from ...det_floor import <check>`` working unchanged.
3. The symbols other tests bind to ON THE ``det_floor`` MODULE OBJECT — ``DET_CHECKS``,
   ``run_det_floor``, ``est_tokens`` are monkeypatched there; ``_clarity_score``,
   ``_count_ac_items``, ``_description_limit`` are imported from there by name — are
   still DEFINED in ``det_floor``, so patching them still reaches the live code path.
4. ``p6_ac_quality``'s bare-name dependencies are bound at module level in its new
   home (a body-only move would have left them undefined at call time).
5. ``det_advisory`` does not import ``det_floor`` at module scope, so the re-export
   direction stays acyclic.
6. ``DET_CHECKS`` still runs all eleven checks, in order.
7. Both modules stay inside the size band the module-size policy sets: at least 100
   lines (no sliver files) and at least 100 lines of REAL headroom under the cap read
   from ``.github/module-size-limit.txt`` — landing back at the cap is the trap this
   story exists to clear.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from module_size_support import read_limit

pytestmark = pytest.mark.unit

_PLAN_REVIEW = Path(__file__).resolve().parents[3] / "src" / "rebar" / "llm" / "plan_review"

# The closed set that crosses the seam: the four never-blocking checks plus the private
# extraction patterns only they use.
_MOVED_CHECKS = (
    "p2_resolution",
    "p3_package_existence",
    "p6_ac_quality",
    "p7_destructive",
)
_MOVED_CONSTANTS = ("_FILE_REF_RE", "_PKG_REF_RE", "_DESTRUCTIVE_RE", "_SAFEGUARD_RE")

# Bound on the det_floor MODULE OBJECT by other tests (monkeypatched or imported by
# name); moving any of them would silently stop those tests exercising anything.
_MODULE_BOUND = (
    "DET_CHECKS",
    "run_det_floor",
    "est_tokens",
    "_clarity_score",
    "_count_ac_items",
    "_description_limit",
)

# ``p6_ac_quality`` calls these as BARE names, so they must be module-level bindings in
# whichever module defines it.
_P6_BARE_NAMES = ("vague_hits_in_line", "_lint_verify_command", "_verify_command_strings")

_MIN_LOC = 100
_MIN_HEADROOM = 100


@pytest.mark.parametrize("name", _MOVED_CHECKS)
def test_never_blocking_checks_are_defined_in_the_extracted_module(name: str) -> None:
    """Each moved check's DEFINING module is the advisory leaf."""
    from rebar.llm.plan_review import det_advisory

    fn = getattr(det_advisory, name)
    assert fn.__module__.endswith("det_advisory"), (
        f"{name} must be DEFINED in det_advisory, not re-exported into it; "
        f"found __module__={fn.__module__!r}"
    )


@pytest.mark.parametrize("name", _MOVED_CHECKS + _MOVED_CONSTANTS)
def test_det_floor_still_exposes_every_moved_name(name: str) -> None:
    """Back-compat: ``det_floor.<symbol>`` resolves to the very same object.

    Tests call ``det_floor.p2_resolution`` / ``det_floor.p7_destructive`` as module
    attributes and import ``p6_ac_quality`` from ``det_floor`` by name; the re-export
    is what keeps those callers unmodified.
    """
    from rebar.llm.plan_review import det_advisory, det_floor

    assert hasattr(det_floor, name), f"det_floor must keep re-exporting {name} after the split"
    assert getattr(det_floor, name) is getattr(det_advisory, name)


@pytest.mark.parametrize("name", _MODULE_BOUND)
def test_module_object_bindings_stay_defined_in_det_floor(name: str) -> None:
    """The half other tests patch/import ON ``det_floor`` is the half that stayed."""
    from rebar.llm.plan_review import det_floor

    obj = getattr(det_floor, name)
    module = getattr(obj, "__module__", "rebar.llm.plan_review.det_floor")
    assert module.endswith("det_floor"), (
        f"{name} is monkeypatched or imported on the det_floor module object; moving it "
        f"would make those tests patch a dead copy (found __module__={module!r})"
    )


@pytest.mark.parametrize("name", _P6_BARE_NAMES)
def test_p6_bare_name_dependencies_are_bound_in_its_new_home(name: str) -> None:
    """``p6_ac_quality`` calls these unqualified, so the move carries their imports."""
    from rebar.llm.plan_review import det_advisory

    assert hasattr(det_advisory, name), (
        f"det_advisory must import {name} at module level — p6_ac_quality calls it as a "
        "bare name and would raise NameError otherwise"
    )


def test_det_advisory_does_not_import_det_floor_at_module_scope() -> None:
    """The re-export is one-directional: det_floor -> det_advisory, never back."""
    tree = ast.parse((_PLAN_REVIEW / "det_advisory.py").read_text(encoding="utf-8"))
    for node in tree.body:  # module scope only; in-body lazy imports are the contract
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("det_floor"):
            pytest.fail("det_advisory must not import det_floor at module scope (import cycle)")
        if isinstance(node, ast.Import):
            assert not any(a.name.endswith("det_floor") for a in node.names)


def test_det_checks_still_runs_all_eleven_checks_in_order() -> None:
    """The split moves definitions, not the floor's composition."""
    from rebar.llm.plan_review.det_floor import DET_CHECKS

    assert [c.__name__ for c in DET_CHECKS] == [
        "p1_readiness_shape",
        "p2_resolution",
        "p3_package_existence",
        "p4_oversize",
        "p5_task_dag",
        "p6_ac_quality",
        "p7_destructive",
        "p8_reviewability",
        "p9_file_impact_coverage",
        "p10_verification_presence",
        "p11_ac_vagueness",
    ]


@pytest.mark.parametrize("filename", ("det_floor.py", "det_advisory.py"))
def test_both_sides_of_the_seam_sit_inside_the_size_band(filename: str) -> None:
    """No sliver file, and real headroom under the cap — measured the way CI measures."""
    cap = read_limit()
    loc = (_PLAN_REVIEW / filename).read_text(encoding="utf-8").count("\n")
    assert loc >= _MIN_LOC, (
        f"{filename} is {loc} lines — a split must not produce a sliver module (minimum {_MIN_LOC})"
    )
    assert loc <= cap - _MIN_HEADROOM, (
        f"{filename} is {loc} lines against a {cap}-line cap — the split must leave at "
        f"least {_MIN_HEADROOM} lines of headroom, not land back at the ceiling"
    )
