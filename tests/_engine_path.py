"""Shared helper: locate the bundled rebar engine dir from the tests tree.

All conftests import this so there is one place to adjust if the layout moves.
The engine lives at ``<repo>/src/rebar/_engine`` and the tests tree at
``<repo>/tests/...``.

THE CANONICAL ROOT (bug ``bd2d-3e31-31d9-4a66``, ``ethnic-rubber-shoveler``)
---------------------------------------------------------------------------
The engine can exist TWICE on disk. Under a NON-editable install
(``uv pip install '.[dev,reviewbot,ui]'`` — what the sweep lanes do) there is the
CHECKOUT copy at ``<repo>/src/rebar/_engine`` and an INSTALLED copy at
``<site-packages>/rebar/_engine``, which is what ``rebar._engine.engine_dir()``
resolves. Under an editable install (``uv sync --locked`` — the merge-gating cell,
and every dev checkout) the two are one directory, so the split is INVISIBLE there.

``sys.modules`` is a flat namespace keyed by NAME, so a canonical
``rebar_reconciler.*`` key can only ever hold ONE of the two copies, and which one
depends on whichever test registered it first. That is not a hazard the suite can
merely be careful about: :func:`engine_dir` here is the ONE canonical root, and
anything a test registers under a canonical ``rebar_reconciler.*`` key must come
from it. The invariant is enforced at runtime by ``tests/conftest.py``'s
``_one_engine_root`` autouse guard (which reads the resulting ``sys.modules``
state, so it sees every registration site regardless of how the module got there)
and statically by ``tests/unit/test_engine_root_convention.py``.

Why the CHECKOUT wins and the installed copy loses:

* The tests tree exists to test the checkout. It monkeypatches engine modules,
  reads their source text, and is coverage-measured against ``src/``; under a
  non-editable install the installed copy is a different file the reviewer never
  diffed.
* It is already the overwhelming incumbent: this helper, ``tests/unit/conftest.py``'s
  ``rebar_reconciler`` shadow ``__path__``, ``tests/unit/rebar_reconciler/conftest.py``
  and some three hundred test modules spell the checkout.
* Bug ``9f0b-3d48-b935-428b`` settles the tie. The shadow package's ``__path__``
  carries the CHECKOUT engine dir, so every ``import_module("rebar_reconciler.X")``
  in the unit tier binds a checkout module. Making the installed copy canonical
  would leave those bindings non-canonical and force the by-path loaders'
  ``__file__`` guard — which is NOT the defect and must stay, per
  ``tests/unit/rebar_reconciler/diffing/test_load_module_identity.py`` — to evict and
  rebuild them, reproducing exactly the two-class-objects state that made
  ``isinstance`` answer False. That failure was observed when bug
  ``ae96-72a9-8145-4c85``'s first attempt repointed unit-tier files at
  ``rebar._engine.engine_dir()``, and it is why that attempt was reverted.

``rebar._engine.engine_dir()`` remains correct for PRODUCTION, which launches the
engine as a subprocess out of the installed package; it is only the tests tree that
must not reach for it.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from functools import cache, lru_cache
from pathlib import Path
from types import ModuleType

# The two path segments that mark any on-disk copy of the engine, whichever install
# it belongs to: ``<anything>/rebar/_engine``.
_ENGINE_MARKER: tuple[str, str] = ("rebar", "_engine")

#: ``sys.modules`` keys that name an engine module (the package itself, or a submodule).
_CANONICAL_PREFIX = "rebar_reconciler"


@lru_cache(maxsize=1)
def repo_root() -> Path:
    # tests/_engine_path.py -> tests -> <repo>
    return Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def engine_dir() -> Path:
    """The ONE canonical engine root for the tests tree: the checkout's copy."""
    return repo_root() / "src" / "rebar" / "_engine"


def acli_path() -> Path:
    return engine_dir() / "rebar_reconciler" / "adapters" / "jira" / "acli.py"


def is_canonical_key(name: str) -> bool:
    """True for ``rebar_reconciler`` and any ``rebar_reconciler.<sub>`` key."""
    return name == _CANONICAL_PREFIX or name.startswith(_CANONICAL_PREFIX + ".")


def engine_root_of(path: str | Path) -> Path | None:
    """The ``.../rebar/_engine`` root ``path`` lives under, or ``None``.

    ``None`` means "not an engine file at all" — the ``tests/unit/rebar_reconciler``
    shadow package and its test subpackages resolve here, and they are legitimate
    holders of a ``rebar_reconciler.*`` key. Only engine files are subject to the
    single-root rule, because only they exist twice.
    """
    parts = Path(path).resolve().parts
    for index in range(len(parts) - 1):
        if (parts[index], parts[index + 1]) == _ENGINE_MARKER:
            return Path(*parts[: index + 2])
    return None


@cache
def _foreign_root(path: str) -> Path | None:
    """The engine root of ``path`` when it is an engine file from a NON-canonical copy.

    Cached on the path STRING: the guard re-reads the same few dozen ``__file__`` values
    after every one of ~19k tests, and each miss costs a ``Path.resolve()`` syscall.
    """
    root = engine_root_of(path)
    if root is None or root == engine_dir().resolve():
        return None
    return root


def foreign_engine_registrations(
    modules: Mapping[str, ModuleType] | None = None,
) -> list[tuple[str, str]]:
    """Every canonical ``rebar_reconciler.*`` binding served by a non-canonical engine copy.

    Returns sorted ``(what, path)`` pairs, where ``what`` is either the ``sys.modules``
    key or ``rebar_reconciler.__path__`` (a foreign entry there makes every LATER
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
        yield (name, str(Path(file_name).resolve()))
    for entry in getattr(module, "__path__", ()) or ():
        if _foreign_root(str(entry)) is not None:
            yield (f"{name}.__path__", str(Path(entry).resolve()))
