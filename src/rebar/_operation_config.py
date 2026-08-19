"""The per-operation, immutable, NON-SECRET configuration authority (RP-04 S1).

One concern: compose ONE ``OperationSnapshot`` per operation — a frozen, serializable
record of the effective non-secret config values, their source-kind provenance, the
selected repository root, and an envelope version — and thread it through the entry
points in **shadow mode**: diagnostic only, it does NOT control execution and no
behavior-bearing consumer is cut over to it here. This is the walking skeleton for the
RP-04 epic; the switchover (and removal of the ``REBAR_OPERATION_SNAPSHOT_SHADOW``
rollback switch) happens in later stories (S7).

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

import dataclasses
import logging
import os
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from rebar._config_coercion import ConfigError, _as_bool
from rebar._store import canonical as _canonical

logger = logging.getLogger(__name__)

ENVELOPE_VERSION: int = 1

SHADOW_ENV: str = "REBAR_OPERATION_SNAPSHOT_SHADOW"

_SOURCE_KINDS = frozenset({"default", "user", "project", "env", "cli"})


def shadow_enabled() -> bool:
    """Whether per-operation shadow snapshots are composed this run.

    Reads :data:`SHADOW_ENV` at call time. UNSET ⇒ ``True`` (enabled by default);
    a canonical false spelling (``false``/``0``/``no``/``off``/``""``) ⇒ ``False``;
    a canonical true spelling (``true``/``1``/``yes``/``on``) ⇒ ``True``. Reuses the
    shared boolean coercion so the switch reads exactly like every other rebar
    boolean; an unrecognized value defaults to enabled (the diagnostic is guarded and
    side-effect-free, so failing open is safe)."""
    raw = os.environ.get(SHADOW_ENV)  # read-via: subsystem-kill-switch
    if raw is None:
        return True
    try:
        return _as_bool(raw, SHADOW_ENV)
    except ConfigError:
        return True


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


def emit_shadow_snapshot(
    *,
    cli_overrides: dict | None = None,
    repo_root: str | os.PathLike[str] | None = None,
    surface: str,
) -> OperationSnapshot | None:
    """Compose + diagnostically log ONE shadow snapshot, guarded, never behavioral.

    Returns the composed snapshot, or ``None`` when shadow mode is disabled or the
    assembly fails. The shadow is diagnostic-only and MUST NOT change legacy behavior:
    ANY exception from assembly/serialization/fingerprint/diagnostic — including a
    :class:`~rebar.config.ConfigError` (or its ``InsecureUrlError`` subclass) from
    ``resolve_with_sources`` on malformed/insecure config — is caught, logged REDACTED
    (only the exception type name — never values, paths, or secrets), and the untouched
    legacy operation continues. Some legacy operations tolerate a config the strict
    schema rejects (e.g. ``bridge setup --reset``, which only clears a section, or a
    completion gate that is off and merely warns); the shadow must never promote that
    into a raised error. This does not contradict ``docs/config.md``'s "malformed config
    fails fast before effects": that fail-fast is the *real* operation's own config read
    (via :func:`compose_operation_snapshot`, which DOES propagate ``ConfigError``); the
    swallow here applies only to this diagnostic shadow, which runs beside — never in
    place of — that real path.

    The emitted diagnostic carries ONLY the envelope version, the distinct source
    kinds, and a redacted (truncated) fingerprint — never input values, secrets, or
    file paths."""
    if not shadow_enabled():
        return None
    try:
        snapshot = compose_operation_snapshot(cli_overrides=cli_overrides, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 — shadow must never break the legacy op
        logger.warning("operation snapshot shadow (%s) skipped: %s", surface, type(exc).__name__)
        return None
    try:
        kinds = sorted({label for keys in snapshot.sources.values() for label in keys.values()})
        logger.debug(
            "operation snapshot shadow (%s): envelope=%d sources=%s fingerprint=%s…",
            surface,
            snapshot.envelope_version,
            kinds,
            snapshot.fingerprint()[:12],
        )
    except Exception as exc:  # noqa: BLE001 — diagnostic must never break the legacy op
        logger.warning(
            "operation snapshot shadow (%s) diagnostic skipped: %s", surface, type(exc).__name__
        )
    return snapshot
