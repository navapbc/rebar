#!/usr/bin/env python3
"""Portable public-API surface drift gate (ticket a454-9285-b999-4ac5).

A ``griffe check``-style drift gate for rebar's **Python public-API surface**, modelled
on the config-key drift gate (``scripts/gen_config_reference.py``) and implemented with
the same self-contained, pure-stdlib introspection the repo already uses in
``tests/unit/test_reuse_surface_doc.py`` — no ``griffe`` (or any new) dependency.

It snapshots the public surface into a committed baseline JSON and, in ``--check`` mode,
re-introspects the live code and fails on any drift: a removed/renamed public symbol, a
changed function signature (a removed/renamed/reordered parameter, a param that gained or
lost a default), a changed class shape (bases, dataclass fields, public-method
signatures), or a changed public constant. The gate runs as a plain unit test under
``make test`` (``tests/unit/test_api_surface_gate.py``) with **no CI-provider
dependency**, so it is operation/test-linked and portable across every supported venue.

## Source of truth (the pinned rule)

The guarded surface is the union, over the pinned ``MODULES`` list below, of each
module's **public members**, where "public member" is:

  * every name in the module's ``__all__`` when it defines one (the ``rebar`` facade and
    ``rebar.signing`` / ``rebar.llm.workflow.executor`` do); otherwise
  * every non-underscore attribute the module **owns** — i.e. whose ``__module__`` is that
    module (or a submodule of it) or which carries no ``__module__`` (module-level
    constants) — excluding imported re-exports and submodules.

``MODULES`` is exactly the ``rebar.*`` facade plus the reuse subsystems documented in
``docs/reuse-surface.md`` / ``docs/api-stability.md``. Adding a documented reuse module
here extends the guard.

Usage:
    python scripts/gen_api_surface.py            # check the committed baseline (default)
    python scripts/gen_api_surface.py --check    # exit non-zero if the baseline is stale
    python scripts/gen_api_surface.py --update    # rewrite the baseline after an intended change
"""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.metadata
import inspect
import json
import sys
from pathlib import Path
from typing import Any

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / "tests" / "unit" / "api_surface_baseline.json"
GEN_CMD = "python scripts/gen_api_surface.py"
DISTRIBUTION = "nava-rebar"
#: The drift line a stale install produces on its own; ``rebar.__version__`` reads dist-info.
_VERSION_DRIFT = "~ CHANGED  rebar.__version__"

#: The pinned public surface: the ``rebar`` facade plus the documented reuse subsystems.
MODULES: tuple[str, ...] = (
    "rebar",
    "rebar.signing",
    "rebar.llm.runner",
    "rebar.llm.contracts",
    "rebar.llm.findings",
    "rebar.llm.prompting.prompts",
    "rebar.llm.workflow.executor",
)

#: Simple immutable types whose value we pin verbatim (guards curated constants such as
#: ``prompts.FRONT_MATTER_KEYS`` and ``prompts.EXECUTION_MODES``).
_PINNED_VALUE_TYPES = (str, int, bool, tuple, frozenset)


def _owns(module: Any, obj: Any) -> bool:
    """True when ``obj`` is defined by ``module`` (or has no owning module: a constant)."""
    owner = getattr(obj, "__module__", None)
    if owner is None:
        return True
    return owner == module.__name__ or owner.startswith(module.__name__ + ".")


def public_names(module: Any) -> list[str]:
    """The module's pinned public member names (``__all__`` when present, else owned)."""
    if hasattr(module, "__all__"):
        return sorted(module.__all__)
    names: list[str] = []
    for name in dir(module):
        if name.startswith("_"):
            continue
        obj = getattr(module, name)
        if inspect.ismodule(obj):
            continue
        if _owns(module, obj):
            names.append(name)
    return sorted(names)


def _params(func: Any) -> list[list[str]] | None:
    """``[name, kind, default]`` per parameter; ``default`` is ``"<none>"`` when required.

    Returns ``None`` when the object has no introspectable signature (e.g. some builtins),
    so a symbol without a signature is still tracked (drift on kind is still detected).
    """
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return None
    out: list[list[str]] = []
    for param in sig.parameters.values():
        default = "<none>" if param.default is inspect.Parameter.empty else repr(param.default)
        out.append([param.name, param.kind.name, default])
    return out


def _return_annotation(func: Any) -> str | None:
    """Normalized return annotation for a callable, or ``None`` when unavailable/empty."""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return None
    annotation = sig.return_annotation
    if annotation is inspect.Signature.empty:
        return None
    if isinstance(annotation, str):
        try:
            literal = ast.literal_eval(annotation)
        except (ValueError, SyntaxError):
            return annotation
        if isinstance(literal, str):
            return literal
        return annotation
    return inspect.formatannotation(annotation)


def _owned_class(obj: Any) -> bool:
    """True when ``obj`` (a class or function) is defined in the rebar package."""
    module = getattr(obj, "__module__", "") or ""
    return module == "rebar" or module.startswith("rebar.")


def _describe_class(obj: type) -> dict[str, Any]:
    """Shape of a public class: bases, dataclass fields, and public-method signatures.

    Only methods defined by **rebar-owned** classes in the MRO are captured; inherited
    builtin/stdlib methods (e.g. ``Exception.add_note``, ``BaseException.with_traceback``)
    are excluded because their introspected signatures drift across Python versions and
    platforms, which would make the baseline non-portable.
    """
    desc: dict[str, Any] = {
        "kind": "class",
        "bases": [b.__name__ for b in obj.__bases__],
    }
    fields = getattr(obj, "__dataclass_fields__", None)
    if fields is not None:
        desc["fields"] = sorted(fields)
    methods: dict[str, Any] = {}
    for klass in obj.__mro__:
        if not _owned_class(klass):
            continue
        for name, member in vars(klass).items():
            if name.startswith("_") and name != "__init__":
                continue
            if not callable(member) or name in methods:
                continue
            func = getattr(member, "__func__", member)
            if not _owned_class(func):
                continue
            methods[name] = {
                "params": _params(member),
                "return": _return_annotation(member),
            }
    desc["methods"] = methods
    return desc


def _describe(obj: Any) -> dict[str, Any]:
    """A stable, JSON-serialisable descriptor of one public symbol."""
    if inspect.isclass(obj):
        return _describe_class(obj)
    if inspect.isroutine(obj) or (callable(obj) and not isinstance(obj, _PINNED_VALUE_TYPES)):
        return {"kind": "callable", "params": _params(obj), "return": _return_annotation(obj)}
    if isinstance(obj, _PINNED_VALUE_TYPES):
        return {"kind": "value", "type": type(obj).__name__, "value": _value_repr(obj)}
    return {"kind": "value", "type": type(obj).__name__, "value": None}


def _value_repr(obj: Any) -> Any:
    """Deterministic repr of a pinned constant (sets are sorted so order never drifts)."""
    if isinstance(obj, frozenset):
        return sorted(repr(item) for item in obj)
    if isinstance(obj, tuple):
        return [repr(item) for item in obj]
    return repr(obj)


def build_surface() -> dict[str, Any]:
    """Introspect every pinned module into ``{module: {symbol: descriptor}}``."""
    surface: dict[str, Any] = {}
    for module_name in MODULES:
        module = importlib.import_module(module_name)
        members: dict[str, Any] = {}
        for name in public_names(module):
            members[name] = _describe(getattr(module, name))
        surface[module_name] = members
    return surface


def render_surface() -> str:
    """The canonical baseline JSON (sorted keys, trailing newline)."""
    return json.dumps(build_surface(), indent=2, sort_keys=True) + "\n"


def _load_baseline() -> dict[str, Any]:
    if not BASELINE_PATH.exists():
        return {}
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def diff_surface(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Human-readable drift lines between two surfaces; empty when identical."""
    drift: list[str] = []
    for module in sorted(set(baseline) | set(current)):
        old = baseline.get(module, {})
        new = current.get(module, {})
        for symbol in sorted(set(old) - set(new)):
            drift.append(f"- REMOVED  {module}.{symbol}")
        for symbol in sorted(set(new) - set(old)):
            drift.append(f"+ ADDED    {module}.{symbol}")
        for symbol in sorted(set(old) & set(new)):
            if old[symbol] != new[symbol]:
                drift.append(f"~ CHANGED  {module}.{symbol}")
    return drift


def declared_version() -> str | None:
    """The static ``[project].version`` in ``pyproject.toml``, or ``None`` when absent."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle).get("project", {}).get("version")
    return version if isinstance(version, str) else None


def installed_version_drift(
    *, installed: str | None = None, declared: str | None = None
) -> str | None:
    """``None`` when the installed distribution matches the tree, else the diagnostic.

    An editable install writes its version into ``.dist-info`` once, so a rebase across a
    version bump leaves ``rebar.__version__`` reporting the old version. The surface gate
    then reads that as API drift, and regenerating the baseline would commit it. A tree
    with no install, or no static version, has nothing to compare and reports ``None``.
    """
    if installed is None:
        try:
            installed = importlib.metadata.version(DISTRIBUTION)
        except importlib.metadata.PackageNotFoundError:
            return None
    if declared is None:
        declared = declared_version()
    if declared is None or installed == declared:
        return None
    return (
        f"The installed {DISTRIBUTION} is {installed}, but pyproject.toml declares "
        f"{declared}.\n"
        f"The editable install recorded its version when it was installed, and a later "
        f"checkout or rebase across a version bump does not refresh it, so "
        f"rebar.__version__ reports {installed}.\n"
        f"Fix: run `make install` in this checkout, then re-run. Do not regenerate "
        f"the API-surface baseline: it would record {installed}."
    )


def _check() -> int:
    baseline = _load_baseline()
    current = build_surface()
    drift = diff_surface(baseline, current)
    if not drift:
        return 0
    try:
        label: Path | str = BASELINE_PATH.relative_to(REPO_ROOT)
    except ValueError:
        label = BASELINE_PATH
    sys.stderr.write(
        f"Public API surface drift detected against the committed baseline ({label}):\n"
    )
    sys.stderr.write("\n".join(drift) + "\n")
    stale_install = installed_version_drift()
    if stale_install is not None:
        sys.stderr.write(f"\n{stale_install}\n")
        if drift == [_VERSION_DRIFT]:
            return 1
    sys.stderr.write(
        "\nIf this change is intentional, refresh the baseline and review the diff:\n"
        f"    {GEN_CMD} --update\n"
    )
    return 1


def _update() -> int:
    stale_install = installed_version_drift()
    if stale_install is not None:
        sys.stderr.write(f"Refusing to update {BASELINE_PATH.name}.\n{stale_install}\n")
        return 2
    BASELINE_PATH.write_text(render_surface(), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Public-API surface drift gate.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check", action="store_true", help="exit non-zero if the committed baseline is stale"
    )
    group.add_argument(
        "--update", action="store_true", help="rewrite the baseline after an intended change"
    )
    args = parser.parse_args(argv)

    if args.update:
        return _update()
    return _check()


if __name__ == "__main__":
    raise SystemExit(main())
