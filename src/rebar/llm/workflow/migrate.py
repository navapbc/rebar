"""Read-time schema_version up-conversion for the workflow DSL (WS-B3).

The DSL evolves the way the event store does: forward-only and never destructive.
Each released DSL version ships ONE immutable, version-pinned JSON Schema at a
stable ``$id`` (``workflow.v1.schema.json``, ``workflow.v2.schema.json``, …) that
is *never edited in place*. To run an older file under a newer rebar, we
**up-convert at read time** through a chain of single-step shims
(``vN -> v(N+1)``) and validate the result against the current schema — we never
rewrite the file on disk. To run a *newer* file under an older rebar is a hard
``WorkflowVersionError`` ("upgrade rebar"), because a forward shim cannot be
invented after the fact.

This mirrors the store's replay model: the bytes on disk are the immutable record;
the in-memory shape is derived. Registering a shim is the only supported way to
change the DSL across a version boundary, and every shim must carry a golden
round-trip test (a vN fixture and its expected v(N+1) output) so the conversion is
pinned, not hand-waved.

``v1`` is the base version and ``v2`` is current, so ``_SHIMS`` holds the single
``v1 -> v2`` up-conversion (a pure version bump — v2 is a superset of v1). Its
golden round-trip and the multi-step chaining machinery are pinned by
``tests/unit/workflow/test_migrate.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from .schema import (
    CURRENT_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    declared_version,
    schema_name_for_version,
)

# A single-step up-conversion shim. Keyed by the SOURCE version string; the
# callable takes a document at that version and returns a NEW document at the next
# version, with ``schema_version`` advanced. Shims must be pure (no in-place
# mutation of the input) and total over valid documents at their source version.
Shim = Callable[[dict[str, Any]], dict[str, Any]]


def _v1_to_v2(doc: dict[str, Any]) -> dict[str, Any]:
    """Up-convert a v1 workflow document to v2.

    v2 is a strict SUPERSET of v1: every v1 step is a leaf (scripted ``uses:`` or
    agentic ``prompt:``), and v2 keeps that shape verbatim while ADDING the
    ``branch``/``loop``/``map`` control constructs. So a v1 file is already a valid
    v2 file apart from its ``schema_version`` stamp — the conversion is a pure
    version bump with NO structural rewrite (the cleanest possible shim, and exactly
    why v1 authoring ergonomics carry forward unchanged). Pure: returns a new dict,
    never mutates ``doc`` (``migrate_to_current`` deep-copies before calling, but we
    keep the contract here too). ``schema_version`` is advanced by the caller; we set
    it explicitly so the shim is correct in isolation (e.g. its golden test).
    """
    out = dict(doc)
    out["schema_version"] = "2"
    return out


# The registry. v1 is the base DSL version; ``_v1_to_v2`` up-converts it to the
# current v2 (a pure version bump — v2 is a superset of v1). Each entry is keyed by
# its SOURCE version and carries a golden round-trip test (see test_migrate.py).
# Ordered by the natural integer order of the keys at migrate time, so a multi-step
# chain (v1->v2->v3) composes deterministically.
_SHIMS: dict[str, Shim] = {"1": _v1_to_v2}


def _next_version(version: str) -> str:
    """The integer-successor version string (``"1" -> "2"``)."""
    return str(int(version) + 1)


def migrate_to_current(doc: dict[str, Any], *, source: str = "<workflow>") -> dict[str, Any]:
    """Up-convert ``doc`` to :data:`CURRENT_SCHEMA_VERSION`, returning a NEW dict.

    * Already current  -> a deep copy, unchanged.
    * Older + reachable -> chained through ``_SHIMS`` one version at a time.
    * Newer than this build, or older with no shim path -> ``WorkflowVersionError``.

    Never mutates ``doc`` and never touches disk; this is the read-time half of the
    store-style "immutable record, derived shape" contract. ``schema_name_for_version``
    is the single gate for the upgrade-rebar case (it raises for a too-new version),
    reused here so the two entry points cannot diverge.
    """
    version = declared_version(doc, source=source)
    # Resolve-and-gate: raises WorkflowVersionError for a version newer than this
    # build understands (the upgrade-rebar case) BEFORE we attempt any chaining.
    schema_name_for_version(version, source=source)

    current = deepcopy(doc)
    # Forward-only chain. Each iteration advances exactly one version; the gate
    # above guarantees we never loop past CURRENT, and a missing shim for a
    # supported-but-older version is a clear, located error.
    while version != CURRENT_SCHEMA_VERSION:
        shim = _SHIMS.get(version)
        if shim is None:
            from rebar.llm.errors import WorkflowVersionError

            raise WorkflowVersionError(
                f"{source}: no migration shim from workflow schema_version {version!r} "
                f"(supported: {', '.join(SUPPORTED_SCHEMA_VERSIONS)}); cannot up-convert"
            )
        nxt = _next_version(version)
        current = shim(current)
        # Defensive: a shim must advance the version it claims to. Catch a buggy
        # shim that forgets to set schema_version rather than spin forever.
        current["schema_version"] = nxt
        version = nxt
    return current


def registered_source_versions() -> tuple[str, ...]:
    """The source versions with a registered up-conversion shim (introspection)."""
    return tuple(sorted(_SHIMS, key=int))
