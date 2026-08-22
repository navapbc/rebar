"""Mapping-config diagnostics for ``rebar doctor``.

Surfaces ``[mapping]`` config that would make a Jira reconcile silently drift, in two
tiers:

* an OFFLINE tier that ALWAYS runs, stdlib-only, against the provider-neutral resolvers
  in :mod:`rebar_reconciler.config` and the loader :mod:`rebar_reconciler.mapping_config`
  -- it never imports any ``adapters.jira*`` package; and
* a best-effort LIVE-DRIFT tier that only attempts a read-only Jira probe when the
  optional ``jira-datacenter`` capability is installed, degrades to a single
  ``unavailable`` finding otherwise (or on ANY probe failure/timeout), degrades a
  PARTIAL probe failure (one axis' read failed while the rest succeeded) to a distinct
  per-axis could-not-check ``unavailable`` finding, and never raises.

Every finding is a plain ``dict``: ``severity`` in ``{"error", "warning", "unavailable"}``,
a stable ``kind``, a human ``detail``, and -- for drift findings -- ``axis``/``value``/``key``.
All heavyweight imports (``_optional``, ``mapping_probe``, the backend/config error types,
the Jira settings/transport) are LAZY, so importing this module stays stdlib-only and never
pulls in an adapter at load time.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from typing import Any


def _ensure_engine_on_path() -> None:
    """Put the bundled engine directory on ``sys.path`` so the top-level
    ``rebar_reconciler`` package resolves.

    The provider-neutral reconciler resolvers ship as ``rebar`` package data under
    ``rebar/_engine/`` (present in the lean core install; only the Jira *adapter* is an
    optional extra), but a bare ``python -m rebar.cli`` process does not add that
    directory to ``sys.path``. Insert it here, idempotently, exactly as the other
    engine-touching commands do -- otherwise the lazy ``from rebar_reconciler import ...``
    below raises ``ModuleNotFoundError`` and crashes ``doctor``.
    """
    from rebar._engine import engine_dir

    eng = str(engine_dir())
    if eng not in sys.path:
        sys.path.insert(0, eng)


# Doctor-side bounded wall-clock ceiling for the best-effort live-drift probe (seconds).
# A module-level attribute so a test may monkeypatch it small.
_PROBE_TIMEOUT_S: float = 10.0

# A sentinel project key that can never be a real project, used to run one resolver pass
# over the default+builtin layer alone (``resolve_for_project`` applies default+builtin
# for any key absent from ``cfg.projects``).
_DEFAULT_SENTINEL = "__rebar_default__"

_KIND_ERROR = "mapping-config-error"
_KIND_STUB = "mapping-config-stub"
_KIND_UNAVAILABLE = "mapping-drift-unavailable"
_KIND_AXIS_UNAVAILABLE = "mapping-drift-axis-unavailable"
_KIND_DRIFT = "mapping-drift"


class _ProbeTimeout(Exception):
    """The live probe exceeded ``_PROBE_TIMEOUT_S`` and was abandoned."""


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def scan_mapping(root: Any = None) -> list[dict[str, Any]]:
    """Run both diagnostic tiers and return all findings."""
    _ensure_engine_on_path()
    from rebar_reconciler import mapping_config as mc

    findings: list[dict[str, Any]] = []
    cfg = None
    try:
        cfg = mc.load_mapping_config(root)
    except mc.MappingConfigError as exc:
        findings.append(_finding("error", _KIND_ERROR, str(exc)))
    else:
        findings.extend(_offline_per_key(cfg, root))
        findings.extend(_stub_warnings(cfg))

    findings.extend(_live_drift(cfg, root))
    return findings


def has_blocking_mapping(findings: list[dict[str, Any]]) -> bool:
    """True when any mapping finding is an error (folds into a non-zero doctor exit)."""
    return any(f.get("severity") == "error" for f in findings)


# ---------------------------------------------------------------------------
# Finding builders
# ---------------------------------------------------------------------------


def _finding(
    severity: str, kind: str, detail: str, *, key: str | None = None, **extra: Any
) -> dict[str, Any]:
    out: dict[str, Any] = {"severity": severity, "kind": kind, "detail": detail}
    if key is not None:
        out["key"] = key
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# Offline tier (ALWAYS runs; stdlib-only; never imports adapters.jira*)
# ---------------------------------------------------------------------------


def _offline_per_key(cfg: Any, root: Any) -> list[dict[str, Any]]:
    """Run every provider-neutral resolver for each project key (plus the default layer),
    emitting one error finding per key whose resolution fails."""
    from rebar_reconciler import config as rc
    from rebar_reconciler import mapping_config as mc

    findings: list[dict[str, Any]] = []
    for key in [*sorted(cfg.projects), _DEFAULT_SENTINEL]:
        try:
            rc.effective_status_map(key, root)
            rc.effective_type_map(key, root)
            rc.assert_type_decisions_complete(key, root)
            rc.effective_link_map(key, root)
        except mc.MappingConfigError as exc:
            findings.append(_finding("error", _KIND_ERROR, str(exc), key=key))
    return findings


def _is_all_empty_stub(layer: Any) -> bool:
    """A project layer that declares nothing at all -- every axis map empty and every
    vocabulary declaration ``None`` -- the likely-stub case."""
    return (
        not layer.status_map
        and not layer.type_map
        and not layer.link_map
        and not layer.priority_map
        and not layer.create_defaults
        and layer.statuses is None
        and layer.issue_types is None
        and layer.link_types is None
        and layer.hierarchy is None
    )


def _stub_warnings(cfg: Any) -> list[dict[str, Any]]:
    """One warning per project whose overlay is an all-empty stub."""
    findings: list[dict[str, Any]] = []
    for key in sorted(cfg.projects):
        if _is_all_empty_stub(cfg.projects[key]):
            findings.append(
                _finding(
                    "warning",
                    _KIND_STUB,
                    f"mapping project {key!r} declares no axis maps or vocabularies "
                    "(likely an empty stub that will silently inherit defaults)",
                    key=key,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Live-drift tier (best-effort; degrades; never raises)
# ---------------------------------------------------------------------------


def _unavailable(reason: str) -> dict[str, Any]:
    return _finding(
        "unavailable",
        _KIND_UNAVAILABLE,
        f"live mapping-drift check skipped: {reason}",
    )


def _axis_unavailable(axis: str, reason: str) -> dict[str, Any]:
    """A PER-AXIS could-not-check finding — the probe as a whole succeeded, but ONE axis
    could not be read, so that axis degrades distinctly (never rendered as drift and
    never silently dropped; Flutter doctor's ``notAvailable`` precedent)."""
    return _finding(
        "unavailable",
        _KIND_AXIS_UNAVAILABLE,
        f"live mapping-drift check could not check the {axis} axis: {reason}",
        axis=axis,
    )


def _live_drift(cfg: Any, root: Any) -> list[dict[str, Any]]:
    """Attempt the live probe under the capability guard and a bounded timeout, folding
    every degradation cause into a single ``unavailable`` finding."""
    from rebar import _optional

    if not _optional.capability_installed("jira_datacenter"):
        return [_unavailable("jira-datacenter extra not installed")]

    try:
        observed = _run_bounded(_build_and_read_probe, _PROBE_TIMEOUT_S)
    except _ProbeTimeout:
        return [_unavailable(f"probe timed out after {_PROBE_TIMEOUT_S}s")]
    except Exception as exc:  # noqa: BLE001 -- fold EVERY cause; never raise
        return [_unavailable(_degrade_reason(exc))]

    if cfg is None:
        return []
    return _drift_findings(cfg, root, observed)


def _run_bounded(fn: Callable[[], Any], timeout: float) -> Any:
    """Run ``fn`` on a daemon worker thread, returning its result if it finishes within
    ``timeout`` seconds. A worker that overruns is ABANDONED (never joined forever) and
    :class:`_ProbeTimeout` is raised; a worker exception is re-raised to the caller."""
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 -- carry any failure back to caller
            box["error"] = exc

    thread = threading.Thread(target=_worker, name="doctor-mapping-probe", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise _ProbeTimeout
    if "error" in box:
        raise box["error"]
    return box["value"]


def _build_and_read_probe() -> tuple[set[str], set[str], set[str] | None]:
    """Build the read-only probe (through the module attribute so tests can monkeypatch
    it) and read the observed status / type / link vocabularies. The link axis is
    ``None`` when the port's link-types read could not check (its fail-soft degradation),
    kept distinct from a genuinely-empty observed set."""
    from rebar_reconciler import mapping_probe

    port = mapping_probe.build_probe()
    statuses = set(port.statuses())
    types = {t["name"] for t in port.issue_types() if t.get("name")}
    raw_links = port.issue_link_types()
    links = None if raw_links is None else set(raw_links)
    return statuses, types, links


def _degrade_reason(exc: Exception) -> str:
    """A tailored reason string for a probe failure, by cause where easy."""
    from rebar_reconciler._backend import BackendEnvError

    from rebar._config_coercion import ConfigError

    if isinstance(exc, BackendEnvError):
        return f"Jira backend not configured: {exc}"
    if isinstance(exc, ConfigError):
        return f"settings error: {exc}"
    return f"{type(exc).__name__}: {exc}"


_AXES: tuple[tuple[str, str], ...] = (
    ("status", "effective_status_map"),
    ("type", "effective_type_map"),
    ("link", "effective_link_map"),
)


def _drift_findings(
    cfg: Any, root: Any, observed: tuple[set[str], set[str], set[str] | None]
) -> list[dict[str, Any]]:
    """Diff each project's RESOLVED target values against the observed vocabulary, one
    error finding per absent value. An axis whose observed set is ``None`` (could not
    check) is degraded to ONE distinct ``unavailable`` finding instead of being diffed —
    a failed read must never report every configured target as drift — while the other
    axes are still checked. Hierarchy is excluded by design."""
    from rebar_reconciler import config as rc
    from rebar_reconciler import mapping_config as mc

    observed_by_axis = dict(zip(("status", "type", "link"), observed, strict=True))
    findings: list[dict[str, Any]] = [
        _axis_unavailable(axis, "the probe could not read the live vocabulary for it")
        for axis, seen in observed_by_axis.items()
        if seen is None
    ]
    for key in sorted(cfg.projects):
        try:
            resolved = {axis: getattr(rc, fn)(key, root) for axis, fn in _AXES}
        except mc.MappingConfigError:
            continue  # already reported by the offline tier
        for axis, seen in observed_by_axis.items():
            if seen is None:
                continue
            for value in sorted(set(resolved[axis].values())):
                if value not in seen:
                    findings.append(_drift(axis, value, key))
    return findings


def _drift(axis: str, value: str, key: str) -> dict[str, Any]:
    return _finding(
        "error",
        _KIND_DRIFT,
        f"configured {axis} value {value!r} for project {key!r} is absent from live Jira",
        key=key,
        axis=axis,
        value=value,
    )
