"""rebar root/config resolution (Python side).

Mirrors ``_engine/rebar-config.sh`` so the library and CLI agree with the bash
engine on repo-root and config-file location.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from dataclasses import fields as _dc_fields
from pathlib import Path
from typing import Any

logger = logging.getLogger("rebar.config")


def repo_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the repository root.

    Order: explicit arg > REBAR_ROOT > PROJECT_ROOT > git toplevel of cwd.
    """
    if explicit is not None:
        return Path(explicit).resolve()
    env = os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if out:
            return Path(out).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.cwd()


def config_file(root: str | os.PathLike[str] | None = None) -> Path | None:
    """First existing of $REBAR_CONFIG, <root>/.rebar/config.conf, <root>/.rebar.conf."""
    env = os.environ.get("REBAR_CONFIG")
    if env and Path(env).is_file():
        return Path(env)
    base = repo_root(root)
    for candidate in (base / ".rebar" / "config.conf", base / ".rebar.conf"):
        if candidate.is_file():
            return candidate
    return None


def tracker_dir(root: str | os.PathLike[str] | None = None) -> Path:
    """Path to the ticket event store (.tickets-tracker), honoring the env override."""
    env = os.environ.get("TICKETS_TRACKER_DIR")
    if env:
        return Path(env)
    return repo_root(root) / ".tickets-tracker"


# ── Typed config (the single source of truth for non-secret settings) ─────────
#
# This is the in-memory schema the config-refinement work (epic a621) builds on:
# a stdlib dataclass (no pydantic-settings — core stays dependency-free) holding
# the CORE config keys. ``from_mapping`` parses a nested mapping (TOML
# ``[tool.rebar]`` shape) into the typed object — coercing types, applying
# defaults, honoring legacy aliases, and WARNING (never silently dropping) on
# unknown keys. The TOML loader + discovery + layering (CLI > env > project >
# user > defaults) and routing the existing reads through this are subsequent
# tasks; ``llm.*`` keys live in the optional ``rebar.llm`` layer (not here) so the
# stdlib core never depends on the agents extra. See docs/config.md.


class ConfigError(ValueError):
    """A config value is invalid. Raised at load time so problems fail fast at one
    site rather than surfacing deep in unrelated logic."""


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off", ""}


def _src(source: str) -> str:
    return f" ({source})" if source else ""


def _as_bool(v: Any, key: str) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ConfigError(f"{key}: expected a boolean, got {v!r}")


def _as_int(v: Any, key: str, *, minimum: int | None = None) -> int:
    if isinstance(v, bool):  # bool is an int subclass — reject to catch e.g. true→1
        raise ConfigError(f"{key}: expected an integer, got boolean {v!r}")
    try:
        i = int(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{key}: expected an integer, got {v!r}") from None
    if minimum is not None and i < minimum:
        raise ConfigError(f"{key}: must be >= {minimum}, got {i}")
    return i


def _as_str(v: Any, key: str) -> str:
    if isinstance(v, (dict, list)):
        raise ConfigError(f"{key}: expected a string, got {type(v).__name__}")
    return str(v)


def _as_choice(v: Any, key: str, choices: set[str]) -> str:
    s = str(v).strip().lower()
    if s not in choices:
        raise ConfigError(f"{key}: expected one of {sorted(choices)}, got {v!r}")
    return s


def _warn_unknown(section: str, leftover: dict, source: str) -> None:
    for k in leftover:
        logger.warning(
            "rebar config%s: unknown key '%s.%s' ignored (typo? see docs/config.md)",
            _src(source),
            section,
            k,
        )


@dataclass
class VerifyConfig:
    require_signature_for_close: bool = False


@dataclass
class TicketConfig:
    display_mode: str = "auto"


@dataclass
class CompactConfig:
    threshold: int = 10


@dataclass
class SyncConfig:
    push: str = "always"  # always | async | off
    pull: str = "on"  # on | off


@dataclass
class McpConfig:
    readonly: bool = False
    allow_llm: bool = False
    allow_jira_sync: bool = False


@dataclass
class ReconcilerConfig:
    jira_cli_timeout: int = 0
    lock_max_retries: int = 5
    deletion_probe_limit: int = 20
    id_guard_bypass_unsafe: bool = False


@dataclass
class JiraConfig:
    url: str = ""
    user: str = ""
    project: str = ""


@dataclass
class ScratchConfig:
    base_dir: str = ""


@dataclass
class Config:
    """The typed core configuration — defaults baked in; build with
    :meth:`from_mapping`. Secrets are NOT here (env/.env only)."""

    verify: VerifyConfig = field(default_factory=VerifyConfig)
    ticket: TicketConfig = field(default_factory=TicketConfig)
    compact: CompactConfig = field(default_factory=CompactConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    reconciler: ReconcilerConfig = field(default_factory=ReconcilerConfig)
    jira: JiraConfig = field(default_factory=JiraConfig)
    scratch: ScratchConfig = field(default_factory=ScratchConfig)

    @classmethod
    def from_mapping(cls, raw: dict | None, *, source: str = "") -> Config:
        """Parse a nested mapping (TOML ``[tool.rebar]`` shape) into a Config.

        Coerces/validates each value, applies defaults for anything absent, honors
        legacy aliases, and WARNS (does not drop silently) on unknown sections/keys.
        Raises :class:`ConfigError` on an invalid value (fail-closed at load)."""
        raw = dict(raw or {})
        known = {f.name for f in _dc_fields(cls)}
        for sect in list(raw):
            if not isinstance(raw[sect], dict):
                raise ConfigError(f"[{sect}]: expected a table/section, got {type(raw[sect]).__name__}")
            if sect not in known:
                logger.warning("rebar config%s: unknown section [%s] ignored", _src(source), sect)
        return cls(
            verify=_parse_verify(raw.get("verify"), source),
            ticket=_parse_ticket(raw.get("ticket"), source),
            compact=_parse_compact(raw.get("compact"), source),
            sync=_parse_sync(raw.get("sync"), source),
            mcp=_parse_mcp(raw.get("mcp"), source),
            reconciler=_parse_reconciler(raw.get("reconciler"), source),
            jira=_parse_jira(raw.get("jira"), source),
            scratch=_parse_scratch(raw.get("scratch"), source),
        )


def _parse_verify(d: dict | None, source: str) -> VerifyConfig:
    d = dict(d or {})
    # Legacy alias: verify.require_verdict_for_close -> require_signature_for_close.
    if "require_verdict_for_close" in d and "require_signature_for_close" not in d:
        logger.warning(
            "rebar config%s: 'verify.require_verdict_for_close' is deprecated; "
            "use 'verify.require_signature_for_close'",
            _src(source),
        )
        d["require_signature_for_close"] = d.pop("require_verdict_for_close")
    d.pop("require_verdict_for_close", None)
    out = VerifyConfig(
        require_signature_for_close=_as_bool(
            d.pop("require_signature_for_close", False), "verify.require_signature_for_close"
        )
    )
    _warn_unknown("verify", d, source)
    return out


def _parse_ticket(d: dict | None, source: str) -> TicketConfig:
    d = dict(d or {})
    out = TicketConfig(display_mode=_as_str(d.pop("display_mode", "auto"), "ticket.display_mode") or "auto")
    _warn_unknown("ticket", d, source)
    return out


def _parse_compact(d: dict | None, source: str) -> CompactConfig:
    d = dict(d or {})
    out = CompactConfig(threshold=_as_int(d.pop("threshold", 10), "compact.threshold", minimum=1))
    _warn_unknown("compact", d, source)
    return out


def _parse_sync(d: dict | None, source: str) -> SyncConfig:
    d = dict(d or {})
    out = SyncConfig(
        push=_as_choice(d.pop("push", "always"), "sync.push", {"always", "async", "off"}),
        pull=_as_choice(d.pop("pull", "on"), "sync.pull", {"on", "off"}),
    )
    _warn_unknown("sync", d, source)
    return out


def _parse_mcp(d: dict | None, source: str) -> McpConfig:
    d = dict(d or {})
    out = McpConfig(
        readonly=_as_bool(d.pop("readonly", False), "mcp.readonly"),
        allow_llm=_as_bool(d.pop("allow_llm", False), "mcp.allow_llm"),
        allow_jira_sync=_as_bool(d.pop("allow_jira_sync", False), "mcp.allow_jira_sync"),
    )
    _warn_unknown("mcp", d, source)
    return out


def _parse_reconciler(d: dict | None, source: str) -> ReconcilerConfig:
    d = dict(d or {})
    out = ReconcilerConfig(
        jira_cli_timeout=_as_int(d.pop("jira_cli_timeout", 0), "reconciler.jira_cli_timeout", minimum=0),
        lock_max_retries=_as_int(d.pop("lock_max_retries", 5), "reconciler.lock_max_retries", minimum=0),
        deletion_probe_limit=_as_int(
            d.pop("deletion_probe_limit", 20), "reconciler.deletion_probe_limit", minimum=1
        ),
        id_guard_bypass_unsafe=_as_bool(
            d.pop("id_guard_bypass_unsafe", False), "reconciler.id_guard_bypass_unsafe"
        ),
    )
    _warn_unknown("reconciler", d, source)
    return out


def _parse_jira(d: dict | None, source: str) -> JiraConfig:
    d = dict(d or {})
    out = JiraConfig(
        url=_as_str(d.pop("url", ""), "jira.url"),
        user=_as_str(d.pop("user", ""), "jira.user"),
        project=_as_str(d.pop("project", ""), "jira.project"),
    )
    _warn_unknown("jira", d, source)
    return out


def _parse_scratch(d: dict | None, source: str) -> ScratchConfig:
    d = dict(d or {})
    out = ScratchConfig(base_dir=_as_str(d.pop("base_dir", ""), "scratch.base_dir"))
    _warn_unknown("scratch", d, source)
    return out
