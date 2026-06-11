#!/usr/bin/env python3
"""Foundational primitives shared by the apply layer.

The floor every typed-apply unit stands on: the ``ApplyResult`` value object the
leaves return, the canonical-identity ``mutation``/``_errors`` module loaders, and
the ``_direction_guard`` that enforces each leaf's declared direction. The leaf
modules (``apply_outbound``/``apply_inbound``), ``leaf_registry`` and the
``applier`` facade all import from here — one-directional, so nothing imports
``applier`` at module scope (which would double-load it under the multi-key
regime and fork ``ApplyResult``/``Mutation`` identity).

``applier`` re-exports every name here so ``applier.<name>`` keeps resolving for
``reconcile.py``'s getattr dispatch table and the ~71 ``applier.<name>`` test refs.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MutationModule = None  # late-loaded mutation module; written by _load_mutation_module()
_ErrorsModule = None  # late-loaded _errors module; written by _load_errors_module()


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Result of a typed-mutation apply() dispatch.

    direction/action mirror the Mutation that was dispatched, so callers can
    confirm which leaf executed without re-reading the input. payload carries
    any leaf-specific return data (empty dict by default for the stub leaves).
    """

    direction: Any
    action: Any
    payload: dict[str, Any]


_MUTATION_KEY = "rebar_reconciler.mutation"


def _load_mutation_module():
    """Lazy-load the mutation module under the canonical dotted sys.modules key.

    Uses the SAME key (``rebar_reconciler.mutation``) as
    invariants.py and differ.py so ``Mutation`` / ``MutationDirection`` /
    ``MutationAction`` retain a single class identity across the reconciler.
    Previously each caller loaded under its own private key, producing distinct
    class objects per module — ``isinstance`` and ``is`` comparisons silently
    crossed boundaries and routed mutations to the wrong leaf.
    """
    global _MutationModule
    if _MutationModule is not None:
        return _MutationModule
    if _MUTATION_KEY in sys.modules:
        _MutationModule = sys.modules[_MUTATION_KEY]
        return _MutationModule
    mut_path = Path(__file__).parent / "mutation.py"
    spec = importlib.util.spec_from_file_location(_MUTATION_KEY, mut_path)
    if spec is None:
        raise FileNotFoundError(f"mutation.py not found at {mut_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MUTATION_KEY] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    _MutationModule = mod
    return mod


def _load_errors_module():
    """Lazy-load the _errors module under the canonical sys.modules key.

    Load-once: prefer an already-registered ``rebar_reconciler_errors`` module so
    the error classes (e.g. RebarIdLabelWriteError) keep a SINGLE identity across
    applier reloads and the rebar_id_audit sibling, which also resolves them under
    this key. (Previously this re-exec'd a fresh module on every reload, forking
    the class identity from rebar_id_audit's guard — see tangly-abbey-smelt.)
    """
    global _ErrorsModule
    if _ErrorsModule is not None:
        return _ErrorsModule
    key = "rebar_reconciler_errors"
    if key in sys.modules:
        _ErrorsModule = sys.modules[key]
        return _ErrorsModule
    err_path = Path(__file__).parent / "_errors.py"
    spec = importlib.util.spec_from_file_location(key, err_path)
    if spec is None:
        raise FileNotFoundError(f"_errors.py not found at {err_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    _ErrorsModule = mod
    return mod


# Re-export error classes so callers can import them from apply_base / applier.
# Internal uses still go through _load_errors_module() to preserve lazy-load
# semantics; these module-level names exist for the public import surface.
_errors_module = _load_errors_module()
StatusMappingError = _errors_module.StatusMappingError
DirectionMismatchError = _errors_module.DirectionMismatchError
UnknownActionError = _errors_module.UnknownActionError
RebarIdLabelWriteError = _errors_module.RebarIdLabelWriteError


def _direction_guard(mutation, expected_direction) -> None:
    """Defense-in-depth: assert mutation.direction matches the leaf's declared
    direction. In normal flow _LEAVES lookup already routes correctly; this
    raises DirectionMismatchError if a leaf is invoked directly with the wrong
    direction (e.g. via the test harness bypassing _LEAVES).

    Compare by string value rather than identity. The reconciler loads
    mutation.py multiple times via importlib (once per importing module), and
    each load creates a distinct MutationDirection enum class. Two enum
    members with the same value but from different class instances are NOT
    identity-equal, so ``is not`` would fire spuriously on filtered passes
    where a Mutation built under one module load reaches a leaf imported
    under another.
    """
    expected_val = expected_direction.value
    actual_val = getattr(mutation.direction, "value", mutation.direction)
    if expected_val != actual_val:
        errs = _load_errors_module()
        raise errs.DirectionMismatchError(
            f"leaf expects direction={expected_val!s}, got direction={actual_val!s}"
        )
