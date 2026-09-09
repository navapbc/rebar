"""Typed payload routing remains limited to approved production cutover sites.

No production module imports the shadow comparator. Only ``outbound_pass`` and
``batch_dispatch`` import ``mutation_payloads``, and documented entry points remain
present. The census uses source and attribute inspection without transport I/O.
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

_SHADOW_ONLY_MODULE_NAME = "payload_shadow"
_TYPED_PAYLOAD_MODULE_NAME = "mutation_payloads"

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

# This story's cutover call sites (ADR 0107 "Cut"/"Delete" step): these are
# the ONLY production modules allowed to reference the typed payload
# dataclasses. Every other named production module above must not.
_CUTOVER_MODULES = (outbound_pass, batch_dispatch)


def test_no_production_module_imports_the_shadow_comparator():
    for module in _PRODUCTION_MODULES:
        source = Path(inspect.getfile(module)).read_text()
        assert _SHADOW_ONLY_MODULE_NAME not in source, (
            f"{module.__name__} references {_SHADOW_ONLY_MODULE_NAME!r} — the shadow "
            "comparator must stay unwired from every production dispatch entry point"
        )


def test_only_the_named_cutover_modules_import_typed_payloads():
    for module in _PRODUCTION_MODULES:
        source = Path(inspect.getfile(module)).read_text()
        references_typed_payloads = _TYPED_PAYLOAD_MODULE_NAME in source
        if module in _CUTOVER_MODULES:
            assert references_typed_payloads, (
                f"{module.__name__} is a named ADR 0107 cutover call site and must "
                f"reference {_TYPED_PAYLOAD_MODULE_NAME!r}"
            )
        else:
            assert not references_typed_payloads, (
                f"{module.__name__} references {_TYPED_PAYLOAD_MODULE_NAME!r} — the typed "
                "payload cutover is scoped to outbound_pass/batch_dispatch only; wiring "
                "spread further than the ADR's scope"
            )


def test_typed_dispatch_leaves_registry_unchanged_shape():
    # 10 live combinations, none touching the two dead-by-design inbound pairs.
    assert len(typed_dispatch._LEAVES) == 10
    assert ("inbound", "delete") not in {(d.value, a.value) for d, a in typed_dispatch._LEAVES}
    assert ("inbound", "probe") not in {(d.value, a.value) for d, a in typed_dispatch._LEAVES}


def test_legacy_dict_bridge_and_apply_entry_points_still_resolve():
    # batch_dispatch._mutation_to_batch_dict is retained per the ADR's
    # "Cut"/"Delete" step — only its two-CREATE-shape ambiguity branch and
    # applier.apply()'s untyped-dict fallback were deleted; the function
    # itself, and MutationShape, stay.
    assert hasattr(batch_dispatch, "_mutation_to_batch_dict")
    assert callable(batch_dispatch._mutation_to_batch_dict)
    # MutationShape Protocol (the existing, declared discrimination mechanism)
    # is retained, per ADR 0107 Decision §4 step 4.
    assert hasattr(apply_base, "MutationShape")
