"""Optional-dependency guard for rebar's extras (epic a88f / WS-J1).

rebar's runtime is deliberately lean: the hard dependencies are ``pyyaml`` (the
workflow DSL loader), ``jsonschema`` and ``referencing`` (the schema-registry /
contract validator) — the three ``[project.dependencies]`` in ``pyproject.toml``.
Heavy capabilities live behind extras and are imported lazily, so ``import rebar``
— and even running a scripted workflow — never pulls the heavy stack:

  * ``[agents]``  — LLM agent steps, the review ops, the workflow agent runner
    (the provider-agnostic pydantic-ai runtime: ``pydantic-ai-slim[anthropic,retries]``
    + json-repair).
  * ``[eval]``    — prompt evals (Inspect AI + promptfoo interop).
  * ``[tracing]`` — the OTLP trace sink. WRITE-ONLY by rule: OpenTelemetry is a
    sink, never read back into a rebar decision (the oracle-discipline rule).
  * ``[grounding]`` — the code-grounding oracle's in-process structural parsing
    (tree-sitter); the contract + harness are stdlib-only, this extra adds only the
    in-process binding run inside the fail-open worker boundary.

``guard_import`` is the single chokepoint that turns a missing extra into ONE
clear, actionable error naming the exact ``pip install`` — instead of an opaque
``ModuleNotFoundError`` deep in a runner. This module is stdlib-only (it must be
importable in the leanest install) and never imports the optional packages itself.
"""

from __future__ import annotations

from importlib import import_module
from importlib.util import find_spec

# extra -> (probe module that proves it's installed, one-line capability blurb).
# The probe is the lightest import-name that is present iff the extra is.
EXTRAS: dict[str, tuple[str, str]] = {
    "agents": ("pydantic_ai", "LLM agent steps, review operations, and the workflow agent runner"),
    "eval": ("inspect_ai", "prompt evaluation (Inspect AI + promptfoo interop)"),
    "tracing": (
        "opentelemetry",
        "the OTLP trace sink (write-only — OpenTelemetry is never read back into a rebar decision)",
    ),
    "grounding": (
        "tree_sitter_language_pack",
        "the code-grounding oracle's in-process structural parsing (tree-sitter) — "
        "the contract + harness are stdlib-only; this extra adds the in-process binding "
        "run inside the fail-open worker boundary",
    ),
}


class OptionalDependencyError(ImportError):
    """A feature needs an extra that is not installed. The message names the exact
    ``pip install nava-rebar[<extra>]`` to run."""


def _install_hint(extra: str) -> str:
    blurb = EXTRAS.get(extra, ("", ""))[1]
    tail = f" — {blurb}" if blurb else ""
    return f"install it with:  pip install 'nava-rebar[{extra}]'{tail}"


def extra_installed(extra: str) -> bool:
    """True if ``extra``'s probe module is importable — pure detection, no import."""
    probe = EXTRAS.get(extra, (None,))[0]
    if not probe:
        return False
    try:
        return find_spec(probe) is not None
    except (ImportError, ValueError):
        return False


def require_extra(extra: str) -> None:
    """Raise :class:`OptionalDependencyError` (naming the install) unless ``extra``
    is installed. Use at a feature boundary before any heavy import."""
    if extra not in EXTRAS:
        raise ValueError(f"unknown extra {extra!r} (known: {', '.join(sorted(EXTRAS))})")
    if not extra_installed(extra):
        raise OptionalDependencyError(
            f"the {extra!r} extra is required for this feature but is not installed; "
            f"{_install_hint(extra)}"
        )


def guard_import(module: str, *, extra: str):
    """Import ``module``, or raise :class:`OptionalDependencyError` naming the extra.

    The single chokepoint for optional imports: ``mod = guard_import(
    'pydantic_ai', extra='agents')``. A missing dependency becomes one
    legible error with the exact ``pip install`` rather than a bare ImportError.
    """
    try:
        return import_module(module)
    except ImportError as exc:
        raise OptionalDependencyError(
            f"{module!r} is required for the {extra!r} extra but is not importable; "
            f"{_install_hint(extra)}"
        ) from exc
