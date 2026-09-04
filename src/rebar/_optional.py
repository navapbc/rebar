"""Optional-dependency guard for rebar's extras (epic a88f / WS-J1).

rebar's runtime is deliberately lean: the hard dependencies are ``pyyaml`` (the
workflow DSL loader), ``jsonschema`` and ``referencing`` (the schema-registry /
contract validator) — the three ``[project.dependencies]`` in ``pyproject.toml``.
Heavy capabilities live behind extras and are imported lazily, so ``import rebar``
— and even running a scripted workflow — never pulls the heavy stack:

  * ``[agents]``  — LLM agent steps, the review ops, the workflow agent runner
    (the provider-agnostic pydantic-ai runtime: ``pydantic-ai-slim[anthropic,retries]``
    + json-repair).
  * ``[tracing]`` — the OTLP trace sink. WRITE-ONLY by rule: OpenTelemetry is a
    sink, never read back into a rebar decision (the oracle-discipline rule).
  * ``[grounding]`` — the code-grounding oracle's in-process structural parsing
    (tree-sitter); the contract + harness are stdlib-only, this extra adds only the
    in-process binding run inside the fail-open worker boundary.
  * ``[metrics]`` — code-health analysis through the lazily imported lizard library;
    scc and jscpd remain external command-line tools detected at runtime.

``guard_import`` is the single chokepoint that turns a missing extra into ONE
clear, actionable error naming the exact ``pip install`` — instead of an opaque
``ModuleNotFoundError`` deep in a runner. This module is stdlib-only (it must be
importable in the leanest install) and never imports the optional packages itself.
"""

from __future__ import annotations

import importlib.metadata
import shutil
from importlib import import_module
from importlib.util import find_spec

# extra -> (probe module that proves it's installed, one-line capability blurb).
# The probe is the lightest import-name that is present iff the extra is.
EXTRAS: dict[str, tuple[str, str]] = {
    "agents": ("pydantic_ai", "LLM agent steps, review operations, and the workflow agent runner"),
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
    "grounding-terraform": (
        "hcl2",
        "the optional Terraform structural grounding tools (python-hcl2) — a per-call "
        "refutation-only session over a bounded whole-module snapshot; absent it, the "
        "Terraform grounding path abstains (no_tool) and non-Terraform paths pull nothing",
    ),
    "metrics": ("lizard", "code-health metrics analyzers"),
    "wiki": (
        "pypandoc",
        "the Jira Data Center Markdown-to-wiki renderer (bundles pandoc in the wheel); "
        "without it the DC rich-text path returns Markdown unchanged",
    ),
    "adf": (
        "marklas",
        "Markdown-aware Jira Cloud ADF conversion (headings/lists/code/marks); "
        "without it the Cloud rich-text functions return their plain-text results",
    ),
    "s3": (
        "git_remote_s3",
        "the awslabs/git-remote-s3 remote helper for an s3:// ticket-store remote",
    ),
}


class OptionalDependencyError(ImportError):
    """A feature needs an extra that is not installed. The message names the exact
    ``pip install nava-rebar[<extra>]`` to run."""


def _install_hint(extra: str) -> str:
    blurb = EXTRAS.get(extra, ("", ""))[1]
    tail = f" — {blurb}" if blurb else ""
    return f"install it with:  pip install 'nava-rebar[{extra}]'{tail}"


def module_available(name: str) -> bool:
    """True if ``name`` is importable, without importing it."""
    try:
        return find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def extra_installed(extra: str) -> bool:
    """True if ``extra``'s probe module is importable — pure detection, no import."""
    probe = EXTRAS.get(extra, (None,))[0]
    if not probe:
        return False
    return module_available(probe)


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


MIN_GIT_REMOTE_S3 = "0.3.2"


def require_s3_helper() -> None:
    """Raise OptionalDependencyError unless the S3 remote helper is installed and new enough.

    Checks two conditions (fail closed):
    1. The git-remote-s3 console script is on PATH (shutil.which).
    2. The installed git-remote-s3 distribution is >= 0.3.2.

    Version is parsed stdlib-only: split on '.', take the first three components,
    extract the leading run of digits from each, and convert to int. A non-numeric
    leading component (e.g. "unknown") is treated as NOT meeting the minimum.
    """
    # Check 1: console script on PATH
    if shutil.which("git-remote-s3") is None:
        raise OptionalDependencyError(
            f"the 's3' extra is required for this feature but is not installed; "
            f"{_install_hint('s3')}"
        )

    # Check 2: distribution version >= 0.3.2
    try:
        version_str = importlib.metadata.version("git-remote-s3")
    except importlib.metadata.PackageNotFoundError:
        raise OptionalDependencyError(
            f"the 's3' extra is required for this feature but is not installed; "
            f"{_install_hint('s3')}"
        ) from None

    # Parse version: split on '.', extract leading digits from each component
    parts = version_str.split(".")
    parsed_version: tuple[int, ...] = ()
    for part in parts[:3]:  # Take first 3 components
        # Extract leading run of digits
        digits = ""
        for char in part:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            # Non-numeric leading component: fail closed
            raise OptionalDependencyError(
                f"git-remote-s3 version {version_str!r} does not meet the minimum "
                f"version {MIN_GIT_REMOTE_S3}; {_install_hint('s3')}"
            )
        parsed_version = (*parsed_version, int(digits))

    # Pad with zeros if fewer than 3 components
    while len(parsed_version) < 3:
        parsed_version = (*parsed_version, 0)

    minimum_version = (0, 3, 2)
    if parsed_version < minimum_version:
        raise OptionalDependencyError(
            f"git-remote-s3 version {version_str!r} does not meet the minimum "
            f"version {MIN_GIT_REMOTE_S3}; {_install_hint('s3')}"
        )


# ── Semantic-capability delegation (RP-05 S4) ────────────────────────────────────────────
# ``require_extra`` / ``guard_import`` / ``EXTRAS`` above remain the historical
# optional-dependency compatibility surface. The descriptive *semantic capability* registry
# (``rebar._capabilities``, ADR 0100 §7) is the newer seam: it maps a semantic capability
# (``agent_runtime``, ``audit_ui``, …) to its extra and a typed missing posture, so a boundary
# can enforce AFTER the selected mode/backend is known instead of extra-by-extra. The thin
# delegators below let callers reach that seam through this module. Imported lazily because
# ``rebar._capabilities`` imports :class:`OptionalDependencyError` from here (avoids an
# import cycle); this module never imports the optional packages themselves.


def capability_installed(key: str) -> bool:
    """True if the semantic capability ``key``'s probe module is importable (no import).

    Delegates to :func:`rebar._capabilities.is_available` — pure detection via
    ``importlib.util.find_spec``, never executing the optional package."""
    from rebar import _capabilities

    return _capabilities.is_available(key)


def require_capability(key: str) -> None:
    """Enforce an ``error``-posture semantic capability at a selected execution boundary.

    Delegates to :func:`rebar._capabilities.require_capability`: raises
    :class:`OptionalDependencyError` (naming the exact ``pip install``) when the capability's
    extra is absent, and :class:`ValueError` for a non-``error`` posture (whose degraded /
    abstain / fallback result the domain component owns, not this guard)."""
    from rebar import _capabilities

    _capabilities.require_capability(key)
