"""In-tree backend registry + selector (S3, epic bbf1).

STUB: signatures pinned, bodies to be implemented. Maps ``config.reconciler.backend``
to a factory ``Callable[[Config], Backend]``. A second backend registers itself
in-tree under ``adapters/<x>/`` (no setuptools entry-points).

Design constraints (see ADR 0035 §(d) + the S3 plan):

* ``register(key)`` is a decorator; it is idempotent for the same (key, factory)
  and raises :class:`BackendRegistryError` on a conflicting factory for a key.
* ``select_backend(config)`` LAZILY imports ``rebar_reconciler.adapters`` inside the
  function (so the JiraBackend factory registers as an import side-effect and there
  is no import cycle — the adapters import registry at *their* top, so registry must
  not import adapters at *its* top), then looks up ``config.reconciler.backend`` and
  constructs the backend. An unknown key raises :class:`BackendRegistryError` naming
  the registered keys.
* ``_reset_registry_for_test()`` snapshots + restores registry state so a test can
  register a throwaway backend without leaking process state.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

# Backend key -> factory ``Callable[[Config], Backend]``. Populated as an
# import side-effect when each adapter's backend module is imported (via a lazy
# ``import rebar_reconciler.adapters`` inside :func:`select_backend`).
_REGISTRY: dict[str, Callable[..., Any]] = {}


class BackendRegistryError(RuntimeError):
    """A backend key is unknown, or a conflicting factory was registered for a key."""


def register(key: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: register ``factory`` under ``key`` and return it unchanged.

    Idempotent for the same factory object under the same key; registering a
    *different* factory object under an already-registered key raises
    :class:`BackendRegistryError`.
    """

    def _decorator(factory: Callable[..., Any]) -> Callable[..., Any]:
        existing = _REGISTRY.get(key)
        if existing is not None and existing is not factory:
            raise BackendRegistryError(
                f"backend key {key!r} is already registered to a different factory "
                f"({existing!r}); refusing to overwrite with {factory!r}"
            )
        _REGISTRY[key] = factory
        return factory

    return _decorator


def select_backend(config: Any) -> Any:
    """Construct the backend named by ``config.reconciler.backend``.

    Lazily imports the adapters package inside the function so backend factories
    register as an import side-effect (importing it at module top would cycle,
    since adapters import this registry). An unknown key raises
    :class:`BackendRegistryError` naming the registered keys.
    """
    import rebar_reconciler.adapters  # noqa: F401  (side-effect: registers factories)

    key = config.reconciler.backend
    factory = _REGISTRY.get(key)
    if factory is None:
        raise BackendRegistryError(
            f"unknown reconciler backend {key!r}; registered keys: {sorted(_REGISTRY)}"
        )
    return factory(config)


@contextmanager
def _reset_registry_for_test() -> Iterator[None]:
    """Snapshot ``_REGISTRY`` on enter and restore it exactly on exit.

    Lets a test register throwaway backends without leaking process state
    (state after == state before).
    """
    snapshot = dict(_REGISTRY)
    try:
        yield
    finally:
        _REGISTRY.clear()
        _REGISTRY.update(snapshot)
