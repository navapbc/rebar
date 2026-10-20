"""Shared helper: locate the engine dir and the ONE canonical reconciler package root.

All conftests import this so there is one place to adjust if the layout moves.
The engine assets (the Jira capability probe and resources) live at
``<repo>/src/rebar/_engine``; the Jira reconciler is the ordinary subpackage
``rebar.reconciler`` at ``<repo>/src/rebar/reconciler``.

THE CANONICAL ROOT (bug ``bd2d-3e31-31d9-4a66``, ``ethnic-rubber-shoveler``)
---------------------------------------------------------------------------
The reconciler package can exist TWICE on disk: the CHECKOUT copy at
``<repo>/src/rebar/reconciler`` and, under a NON-editable install, an INSTALLED copy
at ``<site-packages>/rebar/reconciler``. Under an editable install the two are one
directory, so the split is INVISIBLE there.

``sys.modules`` is a flat namespace keyed by NAME, so a canonical
``rebar.reconciler.*`` key can only ever hold ONE of the two copies. Every test now
reaches the reconciler with a plain ``import`` / ``importlib.import_module``, which
resolves it inside whichever ``rebar`` package the test process imports. That package's
``reconciler`` directory is therefore the one canonical root, and anything registered
under a canonical ``rebar.reconciler.*`` key must come from it — a by-path load of the
other copy would leave two class objects for one name (bug ``ae96-72a9-8145-4c85``).
The invariant is enforced at runtime by ``tests/conftest.py``'s ``_one_engine_root``
autouse guard (which reads the resulting ``sys.modules`` state, so it sees every
registration site regardless of how the module got there) and statically by
``tests/unit/test_engine_root_convention.py``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterable, Mapping
from functools import cache, lru_cache
from pathlib import Path
from types import ModuleType

# The two path segments that mark any on-disk copy of the reconciler package, whichever
# install it belongs to: ``<anything>/rebar/reconciler``.
_ENGINE_MARKER: tuple[str, str] = ("rebar", "reconciler")

#: ``sys.modules`` keys that name an engine module (the package itself, or a submodule).
_CANONICAL_PREFIX = "rebar.reconciler"

# Resolved ONCE at import, with ``os.path`` only, and never recomputed. Import happens at
# collection, long before any test rebinds ``os.name``; a lazily-resolved equivalent would
# be computed on its first CALL, which for the post-test guard can fall inside a rebind
# window where ``Path`` is unusable (see :func:`_engine_root_str`).
# tests/_engine_path.py -> tests -> <repo>
_REPO_ROOT_STR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
_ENGINE_DIR_STR = os.path.join(_REPO_ROOT_STR, "src", "rebar", "_engine")
# The ``reconciler`` directory of the ``rebar`` package this process imports (the checkout's
# ``src/rebar/reconciler`` under an editable install). Located with ``find_spec`` so
# resolving it does not import ``rebar``.
_REBAR_SPEC = importlib.util.find_spec("rebar")
_REBAR_PKG_DIR_STR = (
    _REBAR_SPEC.submodule_search_locations[0]
    if _REBAR_SPEC is not None and _REBAR_SPEC.submodule_search_locations
    else os.path.join(_REPO_ROOT_STR, "src", "rebar")
)
_CANONICAL_ENGINE_ROOT_STR = os.path.realpath(os.path.join(_REBAR_PKG_DIR_STR, "reconciler"))


@lru_cache(maxsize=1)
def repo_root() -> Path:
    return Path(_REPO_ROOT_STR)


@lru_cache(maxsize=1)
def engine_dir() -> Path:
    """The checkout's engine asset dir (``src/rebar/_engine``)."""
    return Path(_ENGINE_DIR_STR)


def canonical_reconciler_root() -> Path:
    """The ONE canonical ``rebar.reconciler`` package root for this test process."""
    return Path(_CANONICAL_ENGINE_ROOT_STR)


def acli_path() -> Path:
    return engine_dir().parent / "reconciler" / "adapters" / "jira" / "acli.py"


def is_canonical_key(name: str) -> bool:
    """True for ``rebar.reconciler`` and any ``rebar.reconciler.<sub>`` key."""
    return name == _CANONICAL_PREFIX or name.startswith(_CANONICAL_PREFIX + ".")


def _engine_root_str(path: str | Path) -> str | None:
    """:func:`engine_root_of` as pure strings, for the post-test guard.

    Deliberately builds NO ``Path``. ``pathlib.Path.__new__`` dispatches on the LIVE
    value of ``os.name``, and a test that rebinds it to ``"nt"`` (the supported way to
    exercise a Windows branch — ``tests/unit/test_spawn_detached.py`` does it) turns
    every later ``Path(...)`` into a ``WindowsPath``, which raises ``NotImplementedError``
    on POSIX. The guard runs during teardown, and ``monkeypatch`` undoes its rebinding
    LAST of the function-scoped fixtures, so the guard is still inside the ``"nt"``
    window and cannot rely on ``os.name`` being honest (bug ``f46c-8a93-2eb9-4913``).

    ``os.path`` and ``os.sep`` are bound once at interpreter start from the REAL platform,
    so they keep telling the truth no matter what ``os.name`` is rebound to.
    """
    parts = os.path.realpath(os.fspath(path)).split(os.sep)
    for index in range(len(parts) - 1):
        if (parts[index], parts[index + 1]) == _ENGINE_MARKER:
            return os.sep.join(parts[: index + 2])
    return None


def engine_root_of(path: str | Path) -> Path | None:
    """The ``.../rebar/reconciler`` root ``path`` lives under, or ``None``.

    ``None`` means "not a reconciler package file at all" (for example a test module
    under ``tests/unit/rebar_reconciler``). Only reconciler package files are subject
    to the single-root rule, because only they exist twice.
    """
    root = _engine_root_str(path)
    return None if root is None else Path(root)


@cache
def _foreign_root(path: str) -> str | None:
    """The reconciler root of ``path`` when it is a package file from a NON-canonical copy.

    Returns a STRING rather than a ``Path`` so the whole guard path stays free of
    ``pathlib`` — see :func:`_engine_root_str` for why that matters.

    Cached on the path STRING: the guard re-reads the same few dozen ``__file__`` values
    after every one of ~19k tests, and each miss costs a ``realpath`` syscall.
    """
    root = _engine_root_str(path)
    if root is None or root == _CANONICAL_ENGINE_ROOT_STR:
        return None
    return root


def foreign_engine_registrations(
    modules: Mapping[str, ModuleType] | None = None,
) -> list[tuple[str, str]]:
    """Every canonical ``rebar.reconciler.*`` binding served by a non-canonical package copy.

    Returns sorted ``(what, path)`` pairs, where ``what`` is either the ``sys.modules``
    key or ``rebar.reconciler.__path__`` (a foreign entry there makes every LATER
    submodule import resolve foreign, so it is the same defect one step earlier).

    This reads the resulting import STATE rather than any call site, which is what
    makes it a complete check: the ~146 places that write
    ``sys.modules[name] = mod``, plus ``setdefault``, plus a plain
    ``importlib.import_module``, plus a ``__path__`` append, all land here.
    """
    namespace = sys.modules if modules is None else modules
    offenders: list[tuple[str, str]] = []
    for name, module in list(namespace.items()):
        if not is_canonical_key(name):
            continue
        offenders.extend(_module_offences(name, module))
    return sorted(set(offenders))


def _module_offences(name: str, module: object) -> Iterable[tuple[str, str]]:
    file_name = getattr(module, "__file__", None)
    if file_name and _foreign_root(str(file_name)) is not None:
        yield (name, os.path.realpath(file_name))
    for entry in getattr(module, "__path__", ()) or ():
        if _foreign_root(str(entry)) is not None:
            yield (f"{name}.__path__", os.path.realpath(entry))
