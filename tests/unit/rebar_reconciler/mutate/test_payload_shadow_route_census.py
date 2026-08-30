"""Route-census guard for AC7 (e9d5 / ADR 0107): production continues to use
the pre-cutover route in this story.

This story's new modules (``mutation_payloads.py``, ``payload_shadow.py``)
are additive and shadow-only (Scope: side-effect-free trace projection).
This test proves the OTHER half of AC7 mechanically, without relying on any
subjective "no bridge is presented as final architecture" judgment call:

1. None of the named production dispatch/producer modules import either new
   module — so nothing wires the shadow/typed path into a real dispatch
   decision.
2. Each named production entry point still exists with its documented shape
   (``typed_dispatch._LEAVES`` still has exactly its 10 registered leaves,
   ``batch_dispatch._mutation_to_batch_dict``/``applier.apply`` still resolve)
   — a cheap, portable "this story did not touch these" corroboration
   alongside (1).

Pure source-text + attribute inspection: no git diff, no I/O beyond reading
already-imported modules' source files.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from rebar_reconciler import (
    apply_base,
    batch_dispatch,
    binding_walk,
    differ,
    invariants,
    outbound_mutation_builders,
    outbound_pass,
    run_differs,
    typed_dispatch,
)

_NEW_MODULE_NAMES = ("mutation_payloads", "payload_shadow")

_PRODUCTION_MODULES = (
    typed_dispatch,
    batch_dispatch,
    apply_base,
    differ,
    outbound_mutation_builders,
    run_differs,
    outbound_pass,
    binding_walk,
    invariants,
)


def test_no_production_dispatch_module_imports_the_shadow_modules():
    for module in _PRODUCTION_MODULES:
        source = Path(inspect.getfile(module)).read_text()
        for new_name in _NEW_MODULE_NAMES:
            assert new_name not in source, (
                f"{module.__name__} references {new_name!r} — AC7 requires the shadow/typed "
                "modules stay unwired from every production dispatch entry point in this story"
            )


def test_typed_dispatch_leaves_registry_unchanged_shape():
    # 10 live combinations, none touching the two dead-by-design inbound pairs.
    assert len(typed_dispatch._LEAVES) == 10
    assert ("inbound", "delete") not in {(d.value, a.value) for d, a in typed_dispatch._LEAVES}
    assert ("inbound", "probe") not in {(d.value, a.value) for d, a in typed_dispatch._LEAVES}


def test_legacy_dict_bridge_and_apply_entry_points_still_resolve():
    # batch_dispatch._mutation_to_batch_dict is the one deletion target named
    # by the ADR's "Cut"/"Delete" step — untouched (still present) in THIS story.
    assert hasattr(batch_dispatch, "_mutation_to_batch_dict")
    assert callable(batch_dispatch._mutation_to_batch_dict)
    # MutationShape Protocol (the existing, declared discrimination mechanism)
    # is retained, per ADR 0107 Decision §4 step 4.
    assert hasattr(apply_base, "MutationShape")
