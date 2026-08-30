"""The per-operation, immutable, NON-SECRET configuration authority (RP-04 S1/S2).

One concern: compose ONE ``OperationSnapshot`` per operation — a frozen, serializable
record of the effective non-secret config values, their source-kind provenance, the
selected repository root, and an envelope version — and thread it through the CLI,
MCP, and shared command/store entry points as the AUTHORITATIVE binding for that
operation (RP-04 S2, ticket 3a08): :func:`compose_and_bind_operation_snapshot` composes
exactly once per operation and binds it to a context-local "active operation" so
downstream store helpers (``rebar.config.tracker_dir`` / ``tickets_branch`` /
``tickets_remote``) consume the SAME already-resolved values instead of re-reading
ambient env/config, for the duration of that operation. LLM-specific resolution (the
``rebar.llm`` / reconciler surfaces) has its own AUTHORITATIVE composer —
:func:`rebar.llm.config_binding.compose_and_bind_llm_config` (ticket ec44) — because
``llm`` is a reserved config section this general snapshot never carries; the
diagnostic-only shadow path (:func:`emit_shadow_snapshot`) that both migrations
started from has been retired now that every production call site is cut over.

The snapshot delegates rather than reimplements:

* root selection → :func:`rebar._config_sources.repo_root`
  (explicit > ``REBAR_ROOT`` > git top-level > cwd),
* precedence + provenance → :func:`rebar.config.resolve_with_sources`
  (defaults < user < project < env < cli),
* canonical serialization / fingerprinting → :mod:`rebar._store.canonical`.

It carries ONLY non-secret material: :meth:`OperationSnapshot.build` rejects any leaf
that is not a JSON primitive (or a nested list/dict of primitives), so a pydantic
``SecretStr``/``SecretBytes`` or a live client object can never enter the snapshot.
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

_SOURCE_KINDS = frozenset({"default", "user", "project", "env", "cli"})


def _freeze(mapping: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Mapping[str, Any]]:
    """Wrap a section→key→value mapping read-only at BOTH levels."""
    return MappingProxyType({sect: MappingProxyType(dict(keys)) for sect, keys in mapping.items()})


def _plain(mapping: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """A plain nested ``dict`` copy of a (possibly proxied) two-level mapping."""
    return {sect: dict(keys) for sect, keys in mapping.items()}


def _validate_jsonish(value: Any, *, path: str) -> None:
    """Reject any leaf that is not a JSON primitive or a nested list/dict of them.

    This is the secret/live-object screen: a pydantic ``SecretStr``/``SecretBytes``,
    ``bytes``, or any arbitrary object is not a JSON primitive and raises
    :class:`TypeError`. ``bool`` is admitted (it is an ``int`` subclass and a JSON
    primitive)."""
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
    """One immutable, serializable, non-secret configuration authority per operation.

    Fields are read-only (the mappings are :class:`~types.MappingProxyType` at both
    levels). Construct via :meth:`build` (validating) or :meth:`from_document`
    (rebuild from :meth:`canonical_document`); :func:`compose_operation_snapshot` is
    the central composer that resolves config and calls :meth:`build`."""

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
        """The validating constructor: reject secret/live material and freeze.

        Every leaf in ``values`` must be a JSON primitive (or a nested list/dict of
        them); a ``SecretStr``/``SecretBytes`` or arbitrary object raises
        :class:`TypeError`. ``sources`` labels must be known source kinds. The stored
        mappings are frozen read-only at both levels."""
        for sect, keys in values.items():
            for key, value in keys.items():
                _validate_jsonish(value, path=f"{sect}.{key}")
        for sect, keys in sources.items():
            for key, label in keys.items():
                if label not in _SOURCE_KINDS:
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
    """Compose the one ``OperationSnapshot`` for an operation (the central composer).

    Delegates every layer: root selection to
    :func:`rebar._config_sources.repo_root`, precedence + provenance to
    :func:`rebar.config.resolve_with_sources`, and (later) serialization to
    :mod:`rebar._store.canonical`. A malformed selected config makes
    ``resolve_with_sources`` raise :class:`~rebar.config.ConfigError`; that propagates
    (fail-fast, before any effect) rather than being caught here."""
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


# ── Authoritative binding (RP-04 S2, ticket 3a08) ──────────────────────────────
#
# The reconciler runtime (``_engine/rebar_reconciler/runtime.py``) already demonstrates
# the pattern this reuses: compose ONE snapshot, derive a binding from it, and hold that
# binding for the whole operation instead of re-resolving ambient state per read. Here
# the "binding" is the snapshot itself, held in a context-local (``contextvars``) slot so
# nested store/config calls within the SAME operation observe it without threading an
# explicit parameter through every intermediate call site.
_active_snapshot: contextvars.ContextVar[OperationSnapshot | None] = contextvars.ContextVar(
    "rebar_active_operation_snapshot", default=None
)


def active_snapshot() -> OperationSnapshot | None:
    """The :class:`OperationSnapshot` bound for the CURRENTLY-EXECUTING operation, or
    ``None`` when no operation has composed-and-bound one (e.g. a bare Python-library
    call made outside the CLI/MCP entry points, or a malformed config that made
    composition fail — see :func:`compose_and_bind_operation_snapshot`). Store/config
    helpers consult this FIRST, before falling back to their own ambient resolution, so
    they see the one value composed for this operation rather than a fresh live read."""
    return _active_snapshot.get()


@contextmanager
def bind_operation_snapshot(snapshot: OperationSnapshot) -> Iterator[OperationSnapshot]:
    """Bind an already-composed *snapshot* as the active operation for the block.

    Not reentrant-aware by itself (that policy lives in
    :func:`compose_and_bind_operation_snapshot`) — this is the low-level primitive:
    set, yield, and ALWAYS reset via the token on exit (including on an exception),
    so a nested binding never leaks past its own scope."""
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
    """Compose the ONE :class:`OperationSnapshot` for an operation and bind it active
    for the block — the authoritative counterpart to :func:`emit_shadow_snapshot`.

    Reentrant BY DESIGN: if an operation snapshot is ALREADY bound (e.g. the CLI's
    top-level dispatch already composed one and a nested command/store seam calls this
    again for the same operation), the EXISTING snapshot is reused verbatim — never
    recomposed — so "compose exactly once per operation" holds even when multiple seams
    call this helper while serving the same operation.

    Fails OPEN, matching :func:`emit_shadow_snapshot`'s existing swallow discipline:
    some legacy operations (e.g. ``bridge setup --reset``, which only clears a
    malformed section) must keep working even when the ambient config does not parse.
    A malformed/insecure config (or any other composition failure) is caught here,
    logged REDACTED (exception type name only), and the block runs with NO snapshot
    bound — downstream store/config helpers then fall back to their own ambient
    resolution exactly as they did before this seam existed, so a real operation that
    NEEDS a valid config still fails fast on ITS OWN read, at the same point it always
    did (AC3); this seam merely stops SUCCESSFUL composition from being re-done."""
    existing = active_snapshot()
    if existing is not None:
        yield existing
        return
    try:
        snapshot = compose_operation_snapshot(cli_overrides=cli_overrides, repo_root=repo_root)
    except Exception as exc:  # fail open; see docstring above
        # WARNING carries only the redacted exception type name (no values/paths/
        # secrets) so it is safe on every surface. DEBUG additionally carries the
        # traceback: an unexpected exception here (e.g. an AttributeError/TypeError
        # bug in resolve_with_sources, not a config-validity ConfigError) is still
        # fully diagnosable by an operator with debug logging enabled, without
        # weakening the fail-open guarantee for the operation itself.
        logger.warning("operation snapshot composition skipped: %s", type(exc).__name__)
        logger.debug("operation snapshot composition failure detail", exc_info=exc)
        yield None
        return
    with bind_operation_snapshot(snapshot):
        yield snapshot


def bind_operation_snapshot_for_tools(mcp: Any) -> Any:
    """Wrap an MCP server so every tool it registers runs under
    :func:`compose_and_bind_operation_snapshot`.

    Applied ONLY to the read/write tool registrars in ``mcp_server.py::build_server``
    (mirroring :func:`rebar._lib_warn.suppress_library_double_advisory`'s exact proxy
    pattern) — NOT to the LLM registrar, which intentionally stays on the diagnostic-only
    shadow path pending ticket ec44. Each tool call composes-and-binds ONE snapshot for
    its own duration (MCP tools have no ambient caller-supplied root, so ``repo_root`` is
    left unset and resolved the same way :func:`emit_shadow_snapshot` already does for
    this surface); nested command/store seams invoked from within the tool body reuse
    this SAME binding via ``compose_and_bind_operation_snapshot``'s reentrancy guarantee,
    rather than recomposing. The returned proxy delegates every attribute other than
    ``tool`` to ``mcp`` unchanged and preserves async tools."""

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
