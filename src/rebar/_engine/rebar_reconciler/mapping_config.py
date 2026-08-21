"""Provider-neutral mapping-config core (epic ravenous-dirt-widgeon / bfe7, S1).

The walking-skeleton foundation every other mapping child stands on. Today the
reconciler's rebar<->target mappings live as hardcoded literals; before any axis
becomes config-driven this module ships the *core alone*:

* a reserved ``[tool.rebar.mapping]`` / ``[mapping]`` config section (recognised by the
  core parser, read raw via :func:`rebar.config.read_reserved_section`);
* a three-layer, PER-KEY deep merge for the axis maps -- built-in default
  ``<-`` the ``default`` block ``<-`` a ``projects.<KEY>`` overlay -- where an overlay
  entry overrides only the key it names and unnamed keys inherit the next-outer layer;
* WHOLESALE, most-specific-wins replacement for the vocabulary declarations (a declared
  list fully supersedes the outer one; it is never unioned);
* fail-closed, OFFLINE validation (config-only, never network); and
* a :class:`Capability` descriptor recording which mapping AXES the target's vocabulary
  actually has.

NO axis is wired into the reconciler here -- later stories (S2-S5) do that, and the
concrete adapter injects the target's built-in default layer. This module stays
strictly PROVIDER-NEUTRAL: it imports nothing from any vendor adapter and contains no
vendor value literal anywhere. Illustrative values in docstrings are neutral
placeholders (``"<status-a>"``, ``"VALUE"``), never real target vocabulary.

Structure of a ``[mapping]`` section (raw, as returned by ``read_reserved_section``)::

    [mapping.default.status_map]        # axis map: local key -> target VALUE (or SKIP)
    open = "VALUE"
    [mapping.default]
    statuses = ["<status-a>", "<status-b>"]   # vocabulary declaration (a string list)
    [mapping.default.hierarchy]         # a str->int mapping
    "<type-a>" = 1
    [mapping.projects.KEY.status_map]   # a per-project overlay, same shape as default
    open = "VALUE"

The five axis maps are ``status_map``, ``type_map``, ``link_map``, ``priority_map`` and
``create_defaults``. The vocabulary declarations are ``statuses``, ``issue_types``,
``link_types`` (string lists) and ``hierarchy`` (a str->int mapping).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from rebar._config_coercion import ConfigError

# The sole non-vocabulary value an axis-map entry may hold: an explicit instruction to
# drop the local key rather than map it onto a target vocabulary value. Always an
# allowed axis-map value regardless of any declared vocabulary.
SKIP: str = "skip"

# The axis-map sub-tables that deep-merge PER KEY across the three layers.
_AXIS_MAPS: tuple[str, ...] = (
    "status_map",
    "type_map",
    "link_map",
    "priority_map",
    "create_defaults",
)


class MappingConfigError(ConfigError):
    """A malformed ``[mapping]`` block, or a resolved layer that fails offline
    validation. A subclass of :class:`rebar._config_coercion.ConfigError` so the whole
    config load path fails closed on it exactly as it does for every other config
    error."""


@dataclass(frozen=True)
class Capability:
    """Which mapping AXES the target's vocabulary actually has. Data ABOUT the target,
    injected by the concrete adapter; the core stays neutral. Every axis defaults to
    present -- a fully capable target -- so an unspecified capability never spuriously
    gates a configured map."""

    has_types: bool = True
    has_transitions: bool = True
    has_hierarchy: bool = True
    has_link_types: bool = True
    has_priorities: bool = True


@dataclass(frozen=True)
class MappingLayer:
    """One layer of mapping data (a built-in default, the ``default`` block, or a single
    ``projects.<KEY>`` overlay).

    The five axis maps each default to an empty mapping. The four vocabulary
    declarations each default to ``None``, meaning "undeclared -- inherit the next-outer
    layer's declaration" (distinct from an empty list, which declares an EMPTY
    vocabulary)."""

    status_map: Mapping[str, str] = field(default_factory=dict)
    type_map: Mapping[str, str] = field(default_factory=dict)
    link_map: Mapping[str, str] = field(default_factory=dict)
    priority_map: Mapping[str, str] = field(default_factory=dict)
    create_defaults: Mapping[str, Any] = field(default_factory=dict)

    statuses: tuple[str, ...] | None = None
    issue_types: tuple[str, ...] | None = None
    link_types: tuple[str, ...] | None = None
    hierarchy: Mapping[str, int] | None = None


@dataclass(frozen=True)
class MappingConfig:
    """The parsed ``[mapping]`` overlay: the ``default`` block plus the per-project
    overlays. An absent section yields an empty ``default`` and empty ``projects`` --
    a no-op overlay reproducing today's behaviour."""

    default: MappingLayer = field(default_factory=MappingLayer)
    projects: Mapping[str, MappingLayer] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Parsing (raw reserved section -> typed layers), fail-closed
# ---------------------------------------------------------------------------


def _parse_axis_map(raw: Any, *, where: str) -> dict[str, str]:
    """Parse one axis-map sub-table: local key -> a target VALUE string, or :data:`SKIP`.
    Any other value type fails closed."""
    if not isinstance(raw, Mapping):
        raise MappingConfigError(f"[mapping.{where}]: expected a table, got {type(raw).__name__}")
    out: dict[str, str] = {}
    for key, val in raw.items():
        if val == SKIP:
            out[str(key)] = SKIP
        elif isinstance(val, str):
            out[str(key)] = val
        else:
            raise MappingConfigError(
                f"[mapping.{where}]: value for {key!r} must be a string "
                f"or {SKIP!r}, got {type(val).__name__}"
            )
    return out


def _parse_vocab(raw: Any, *, where: str) -> tuple[str, ...]:
    """Parse a vocabulary declaration: a list of strings."""
    if not isinstance(raw, (list, tuple)):
        raise MappingConfigError(
            f"[mapping].{where}: expected a list of strings, got {type(raw).__name__}"
        )
    for item in raw:
        if not isinstance(item, str):
            raise MappingConfigError(
                f"[mapping].{where}: every entry must be a string, got {type(item).__name__}"
            )
    return tuple(raw)


def _parse_hierarchy(raw: Any, *, where: str) -> dict[str, int]:
    """Parse a ``hierarchy`` declaration: a str -> int mapping."""
    if not isinstance(raw, Mapping):
        raise MappingConfigError(f"[mapping.{where}]: expected a table, got {type(raw).__name__}")
    out: dict[str, int] = {}
    for key, val in raw.items():
        # bool is an int subclass but is never a valid hierarchy rank.
        if not isinstance(val, int) or isinstance(val, bool):
            raise MappingConfigError(
                f"[mapping.{where}]: rank for {key!r} must be an integer, got {type(val).__name__}"
            )
        out[str(key)] = val
    return out


def _parse_layer(raw: Any, *, where: str) -> MappingLayer:
    """Parse one layer table into a :class:`MappingLayer`, failing closed on any
    malformed block. An absent/empty layer yields an all-default layer."""
    if raw is None:
        return MappingLayer()
    if not isinstance(raw, Mapping):
        raise MappingConfigError(f"[mapping.{where}]: expected a table, got {type(raw).__name__}")

    axes: dict[str, dict[str, str]] = {
        name: _parse_axis_map(raw[name], where=f"{where}.{name}")
        for name in _AXIS_MAPS
        if name in raw
    }

    def _vocab(name: str) -> tuple[str, ...] | None:
        if name not in raw:
            return None
        return _parse_vocab(raw[name], where=f"{where}.{name}" if where else name)

    hierarchy: Mapping[str, int] | None = None
    if "hierarchy" in raw:
        hierarchy = _parse_hierarchy(raw["hierarchy"], where=f"{where}.hierarchy")

    return MappingLayer(
        status_map=axes.get("status_map", {}),
        type_map=axes.get("type_map", {}),
        link_map=axes.get("link_map", {}),
        priority_map=axes.get("priority_map", {}),
        create_defaults=axes.get("create_defaults", {}),
        statuses=_vocab("statuses"),
        issue_types=_vocab("issue_types"),
        link_types=_vocab("link_types"),
        hierarchy=hierarchy,
    )


def load_mapping_config(root: Any = None) -> MappingConfig:
    """Read and parse the reserved ``[mapping]`` section for the repo at ``root`` (the
    same user-then-project file discovery every other config layer uses).

    An absent section yields an empty overlay (empty ``default``, empty ``projects``) --
    today's behaviour. Any malformed block fails closed with
    :class:`MappingConfigError`. ``rebar.config`` is imported lazily, mirroring the
    reconciler's deferred-import convention (the core never imports it at module load)."""
    from rebar import config as _config

    raw = _config.read_reserved_section("mapping", root)
    if not isinstance(raw, Mapping) or not raw:
        return MappingConfig()

    default = _parse_layer(raw.get("default"), where="default")

    projects_raw = raw.get("projects", {})
    if not isinstance(projects_raw, Mapping):
        raise MappingConfigError(
            f"[mapping.projects]: expected a table, got {type(projects_raw).__name__}"
        )
    projects = {
        str(key): _parse_layer(val, where=f"projects.{key}") for key, val in projects_raw.items()
    }

    return MappingConfig(default=default, projects=projects)


# ---------------------------------------------------------------------------
# Resolution (three-layer per-key merge + wholesale vocabulary replacement)
# ---------------------------------------------------------------------------


def _merge_axis(builtin: Mapping[str, str], *overlays: Mapping[str, str]) -> dict[str, str]:
    """Deep-merge one axis map PER KEY, outermost layer first. Each overlay overrides
    only the keys it names; unnamed keys inherit the next-outer layer."""
    merged = dict(builtin)
    for overlay in overlays:
        merged.update(overlay)
    return merged


def _pick_vocab(*layers: Any) -> Any:
    """WHOLESALE, most-specific-wins vocabulary pick: return the innermost layer whose
    declaration is not ``None`` (never unioned). ``layers`` is passed
    most-specific-first."""
    for value in layers:
        if value is not None:
            return value
    return None


def resolve_for_project(
    config: MappingConfig, project_key: Any, *, builtin: MappingLayer
) -> MappingLayer:
    """Resolve the EFFECTIVE mapping layer for ``project_key``.

    Axis maps deep-merge PER KEY: ``builtin <- config.default <- projects[project_key]``.
    Vocabulary declarations replace WHOLESALE, most-specific wins: a declared list/table
    on the project overlay supersedes the ``default`` block's, which supersedes the
    built-in's; a layer declaring ``None`` inherits the next-outer declaration.

    ``project_key`` absent from ``config.projects`` (or ``None``) means only
    ``builtin <- default`` applies."""
    default = config.default
    project = config.projects.get(project_key) if project_key is not None else None
    if project is None:
        project = MappingLayer()

    axes = {
        name: _merge_axis(getattr(builtin, name), getattr(default, name), getattr(project, name))
        for name in _AXIS_MAPS
    }

    return MappingLayer(
        **axes,
        statuses=_pick_vocab(project.statuses, default.statuses, builtin.statuses),
        issue_types=_pick_vocab(project.issue_types, default.issue_types, builtin.issue_types),
        link_types=_pick_vocab(project.link_types, default.link_types, builtin.link_types),
        hierarchy=_pick_vocab(project.hierarchy, default.hierarchy, builtin.hierarchy),
    )


# ---------------------------------------------------------------------------
# Offline validation (config-only, fail-closed)
# ---------------------------------------------------------------------------


def declared_status_names(config: MappingConfig) -> set[str]:
    """Every target status NAME the config DECLARES: the non-:data:`SKIP`
    ``status_map`` values plus any declared ``statuses`` vocabulary, taken across the
    ``default`` block AND every ``projects.<KEY>`` overlay. Provider-neutral (no vendor
    literal): the caller unions this with the target's built-in status names. Reused by
    ``fetcher._known_jira_statuses`` so a per-project remap onto a custom status name is
    recognised rather than flagged as unmapped."""
    names: set[str] = set()
    for layer in (config.default, *config.projects.values()):
        names.update(v for v in layer.status_map.values() if v != SKIP)
        names.update(layer.statuses or ())
    return names


def _check_vocab(axis: Mapping[str, str], vocab: Any, *, name: str) -> None:
    """Every non-:data:`SKIP` value of ``axis`` must fall inside ``vocab`` when a
    vocabulary is declared (not ``None``). ``SKIP`` is always allowed."""
    if vocab is None:
        return
    allowed = set(vocab)
    for key, val in axis.items():
        if val == SKIP:
            continue
        if val not in allowed:
            raise MappingConfigError(
                f"{name}[{key!r}] = {val!r} is not a declared {name.split('_')[0]} value"
            )


def _check_capability(axis: Mapping[str, Any], present: bool, *, name: str, axis_kind: str) -> None:
    """A non-empty ``axis`` may not reference a capability-absent axis kind."""
    if axis and not present:
        raise MappingConfigError(f"{name} is configured but the target has no {axis_kind} axis")


def validate(resolved: MappingLayer, capability: Capability) -> None:
    """OFFLINE validation of a resolved layer against a :class:`Capability` (config
    only, never network). Fail-closed with :class:`MappingConfigError`.

    Rules:

    * axis-map values (other than :data:`SKIP`) must fall inside the effective
      vocabulary when one is declared -- ``status_map`` in ``statuses``, ``type_map``
      in ``issue_types``, ``link_map`` in ``link_types``. ``priority_map`` has no
      vocabulary list (capability-gated only); ``create_defaults`` is ungated.
    * a capability-absent axis may not be referenced: a non-empty ``type_map`` needs
      ``has_types``; ``link_map`` needs ``has_link_types``; ``priority_map`` needs
      ``has_priorities``; a declared ``hierarchy`` needs ``has_hierarchy``.
      ``status_map`` and ``create_defaults`` are ungated (and ``has_transitions`` gates
      a future axis with no map here).
    * :data:`SKIP` is always an allowed axis-map value regardless of vocabulary."""
    _check_capability(resolved.type_map, capability.has_types, name="type_map", axis_kind="type")
    _check_capability(
        resolved.link_map, capability.has_link_types, name="link_map", axis_kind="link-type"
    )
    _check_capability(
        resolved.priority_map,
        capability.has_priorities,
        name="priority_map",
        axis_kind="priority",
    )
    if resolved.hierarchy is not None and not capability.has_hierarchy:
        raise MappingConfigError("hierarchy is declared but the target has no hierarchy axis")

    _check_vocab(resolved.status_map, resolved.statuses, name="status_map")
    _check_vocab(resolved.type_map, resolved.issue_types, name="type_map")
    _check_vocab(resolved.link_map, resolved.link_types, name="link_map")
