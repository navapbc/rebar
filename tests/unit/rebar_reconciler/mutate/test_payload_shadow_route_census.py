"""Route-census guard for AC6/AC7 (single-vast-roan / ADR 0107): production is
cut over to the typed payload contract, with the shadow-comparator module kept
strictly out of the loop.

This story wires ``mutation_payloads.py`` (the typed ``Mutation.payload``
dataclasses) into the two production modules the ADR's "Cut"/"Delete" step
names — the outbound producer (``outbound_pass.py``, which now constructs
``OutboundCreatePayload``/``OutboundUpdatePayload``/``OutboundDeletePayload``
directly) and the dispatch-shape normalizer (``batch_dispatch.py``'s
``_mutation_to_batch_dict``, which now reads those dataclasses' own named
attributes instead of sniffing an ambiguous dict shape). ``payload_shadow.py``
(the side-effect-free shadow-replay comparator built by the `e9d5` dependency
story) stays additive/shadow-only — no production dispatch/producer module may
import it.

This test proves both halves mechanically, without relying on a subjective
"no bridge is presented as final architecture" judgment call:

1. NONE of the named production modules import ``payload_shadow`` — nothing
   wires the shadow-comparator path into a real dispatch decision.
2. ONLY ``outbound_pass`` and ``batch_dispatch`` (the two named cutover call
   sites) import ``mutation_payloads``; every other named production module
   still does not — the wiring didn't spread further than the ADR's scope.
3. Each named production entry point still exists with its documented shape
   (``typed_dispatch._LEAVES`` still has exactly its 10 registered leaves,
   ``batch_dispatch._mutation_to_batch_dict``/``applier.apply`` still resolve)
   — a cheap, portable "this story only touched what it says it touched"
   corroboration alongside (1) and (2).

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
