"""Bind one immutable, non-secret configuration snapshot per operation.

CLI, MCP, command, and store calls share its resolved values and provenance
through a context-local binding. Root selection, layered resolution, and canonical
serialization delegate to their existing seams. JSON-only validation excludes
secrets and live objects. LLM configuration uses its separate reserved-section
composer.
"""

from __future__ import annotations

import contextvars
import dataclasses
import functools
import inspect
import logging
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from types import MappingProxyType
from typing import Any

from rebar._config_coercion import ConfigError
from rebar._store import canonical as _canonical

logger = logging.getLogger(__name__)

ENVELOPE_VERSION: int = 1


@functools.cache
def _source_kinds() -> frozenset[str]:
    """Cache known source kinds from ``LAYER_ORDER`` without an import cycle.

    Validation needs membership, so losing layer order is harmless.
    """
    from rebar.config import LAYER_ORDER

    return frozenset(LAYER_ORDER)


def _freeze(mapping: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Mapping[str, Any]]:
    """Wrap a section→key→value mapping read-only at BOTH levels."""
    return MappingProxyType({sect: MappingProxyType(dict(keys)) for sect, keys in mapping.items()})


def _plain(mapping: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """A plain nested ``dict`` copy of a (possibly proxied) two-level mapping."""
    return {sect: dict(keys) for sect, keys in mapping.items()}


def _validate_jsonish(value: Any, *, path: str) -> None:
    """Reject secrets and live objects outside nested JSON primitives.

    ``bool`` remains valid; non-string dictionary keys and other leaves raise.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _validate_jsonish(item, path=f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"non-string key at {path}: {type(key).__name__}")
            _validate_jsonish(item, path=f"{path}.{key}")
        return
    raise TypeError(f"non-serializable value at {path}: {type(value).__name__}")


@dataclasses.dataclass(frozen=True)
class OperationProjection:
    """An immutable view of a snapshot restricted to named sections."""

    values: Mapping[str, Mapping[str, Any]]
    sources: Mapping[str, Mapping[str, str]]


@dataclasses.dataclass(frozen=True)
class OperationSnapshot:
    """Immutable operation config with frozen values, sources, root, and version.

    ``build`` validates; ``from_document`` restores canonical data.
    """

    envelope_version: int
    repo_root: str
    values: Mapping[str, Mapping[str, Any]]
    sources: Mapping[str, Mapping[str, str]]

    @classmethod
    def build(
        cls,
        *,
        envelope_version: int,
        repo_root: str,
        values: Mapping[str, Mapping[str, Any]],
        sources: Mapping[str, Mapping[str, str]],
    ) -> OperationSnapshot:
        """Reject non-JSON values and unknown source kinds, then freeze both maps."""
        for sect, keys in values.items():
            for key, value in keys.items():
                _validate_jsonish(value, path=f"{sect}.{key}")
        for sect, keys in sources.items():
            for key, label in keys.items():
                if label not in _source_kinds():
                    raise ValueError(f"unknown source kind at {sect}.{key}: {label!r}")
        return cls(
            envelope_version=envelope_version,
            repo_root=repo_root,
            values=_freeze(values),
            sources=_freeze(sources),
        )

    @classmethod
    def from_document(cls, doc: dict) -> OperationSnapshot:
        """Rebuild a snapshot from a :meth:`canonical_document`.

        Raises :class:`~rebar.config.ConfigError` when the document's envelope version
        does not match this build's :data:`ENVELOPE_VERSION`."""
        version = doc["envelope_version"]
        if version != ENVELOPE_VERSION:
            raise ConfigError(
                f"operation snapshot envelope version {version!r} != {ENVELOPE_VERSION}"
            )
        return cls.build(
            envelope_version=version,
            repo_root=doc["repo_root"],
            values=doc["values"],
            sources=doc["sources"],
        )

    def canonical_document(self) -> dict:
        """The hashed document — plain nested dicts, not proxies."""
        return {
            "envelope_version": self.envelope_version,
            "repo_root": self.repo_root,
            "values": _plain(self.values),
            "sources": _plain(self.sources),
        }

    def canonical_bytes(self) -> bytes:
        """The canonical committed bytes of :meth:`canonical_document`."""
        return _canonical.canonical_bytes(self.canonical_document())

    def fingerprint(self) -> str:
        """The stable 64-hex sha256 content hash of :meth:`canonical_document`."""
        return _canonical.content_hash(self.canonical_document())

    def project(self, *sections: str) -> OperationProjection:
        """An immutable projection exposing only the named sections.

        Raises :class:`KeyError` for a section not present in this snapshot."""
        for sect in sections:
            if sect not in self.values:
                raise KeyError(sect)
        wanted = set(sections)
        return OperationProjection(
            values=_freeze({s: v for s, v in self.values.items() if s in wanted}),
            sources=_freeze({s: v for s, v in self.sources.items() if s in wanted}),
        )


def compose_operation_snapshot(
    *,
    cli_overrides: dict | None = None,
    repo_root: str | os.PathLike[str] | None = None,
) -> OperationSnapshot:
    """Resolve one snapshot using the shared root and layered config seams.

    Propagate malformed-config errors before the operation takes effect.
    """
    from rebar import _config_sources
    from rebar import config as _config

    root = _config_sources.repo_root(repo_root)
    config, sources, _project = _config.resolve_with_sources(root, cli_overrides=cli_overrides)
    values = dataclasses.asdict(config)
    return OperationSnapshot.build(
        envelope_version=ENVELOPE_VERSION,
        repo_root=str(root),
        values=values,
        sources=sources,
    )


# Bind one snapshot context-locally so nested store/config calls reuse its values.
_active_snapshot: contextvars.ContextVar[OperationSnapshot | None] = contextvars.ContextVar(
    "rebar_active_operation_snapshot", default=None
)


def active_snapshot() -> OperationSnapshot | None:
    """Return this operation's bound snapshot, or ``None`` for ambient fallback."""
    return _active_snapshot.get()


@contextmanager
def bind_operation_snapshot(snapshot: OperationSnapshot) -> Iterator[OperationSnapshot]:
    """Bind a snapshot for this block and always reset its context token."""
    token = _active_snapshot.set(snapshot)
    try:
        yield snapshot
    finally:
        _active_snapshot.reset(token)


@contextmanager
def compose_and_bind_operation_snapshot(
    *,
    cli_overrides: dict | None = None,
    repo_root: str | os.PathLike[str] | None = None,
) -> Iterator[OperationSnapshot | None]:
    """Compose once per operation, reusing an existing context binding.

    Composition failure logs only its exception type and yields no snapshot so
    legacy cleanup can proceed. A later config-dependent read still raises at
    its normal boundary.
    """
    existing = active_snapshot()
    if existing is not None:
        yield existing
        return
    try:
        snapshot = compose_operation_snapshot(cli_overrides=cli_overrides, repo_root=repo_root)
    except Exception as exc:  # fail open; see docstring above
        # Warn with the exception type only; DEBUG retains the traceback without
        # exposing config values in the ordinary warning.
        logger.warning("operation snapshot composition skipped: %s", type(exc).__name__)
        logger.debug("operation snapshot composition failure detail", exc_info=exc)
        yield None
        return
    with bind_operation_snapshot(snapshot):
        yield snapshot


def bind_operation_snapshot_for_tools(mcp: Any) -> Any:
    """Bind one config snapshot around each registered MCP read/write tool.

    Nested calls reuse it; the proxy preserves async tools and delegates other
    attributes. LLM tools use their separate config binding.
    """

    class _BindingRegistrar:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable], Any]:
            inner_deco = self._inner.tool(*args, **kwargs)

            def deco(fn: Callable) -> Any:
                if inspect.iscoroutinefunction(fn):

                    @functools.wraps(fn)
                    async def awrapper(*a: Any, **k: Any) -> Any:
                        with compose_and_bind_operation_snapshot():
                            return await fn(*a, **k)

                    return inner_deco(awrapper)

                @functools.wraps(fn)
                def wrapper(*a: Any, **k: Any) -> Any:
                    with compose_and_bind_operation_snapshot():
                        return fn(*a, **k)

                return inner_deco(wrapper)

            return deco

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    return _BindingRegistrar(mcp)
