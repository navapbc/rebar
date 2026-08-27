"""MCP client-config diagnostics for ``rebar doctor``.

Answers one operator question the rest of ``doctor`` cannot: *will the client I am
about to launch actually reach the rebar MCP server?* Two failure modes make the
``rebar`` server silently vanish from a client's tool list, and both are invisible
from inside the server:

* **The credential is not there.** The documented setup (``docs/mcp-client-setup.md``,
  ``examples/mcp-clients/``) tells the operator to ``export`` a per-client PAT. A bare
  ``export`` is TRANSIENT — it dies with the shell it was typed in, and is persisted
  nowhere. A client launched from any other shell (or a later one) then resolves the
  bearer to nothing, fails auth, and drops the server. That is
  :data:`KIND_PAT_UNRESOLVABLE`.
* **The config names the wrong variable.** A config may reference a bearer env var that
  is not this project's canonical name for that client (a hand-edited or
  copied-from-elsewhere entry). It can even be *resolvable* and still be wrong: the
  operator exports the canonical name, the config reads a different one, and the two
  never meet. That is :data:`KIND_STALE_PAT_ENV_NAME`, and it is reported independently
  of whether the named variable happens to be set.

Fixing either alone can leave the server omitted, so both are always reported.

**No credential VALUE ever enters a finding.** Configs are read for env-var *names*
only, and a name is resolved against the environment purely as a truthiness test — the
value is never bound to a name, formatted, or returned. A header that embeds a literal
instead of referencing a variable is reported by *kind* (:data:`KIND_PAT_LITERAL`)
without echoing the header.

The scan is pure and OS-agnostic: stdlib only (``tomllib``/``json``), no subprocess, no
network, no platform-specific mechanism. ``home`` and ``env`` are injectable so the whole
surface is testable without monkeypatching. Every degradation — a missing config, an
unparseable one, a config with no ``rebar`` entry — becomes a finding, never an exception.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import tomllib

# The MCP server entry these clients are expected to declare for rebar.
SERVER_NAME = "rebar"

# The canonical per-client bearer env-var names. Single source of truth for the
# "stale name" comparison; they match the box-side names in
# infra/runbooks/mcp-client-pats.md and the committed examples/mcp-clients/ configs.
CANONICAL_PAT_ENV: dict[str, str] = {
    "codex": "MCP_CLIENT_PAT_CODEX",
    "copilot": "MCP_CLIENT_PAT_COPILOT",
    "claude": "MCP_CLIENT_PAT_CLAUDE",
}

# Deterministic report order (Codex first — the client this diagnostic was written for).
CLIENT_ORDER: tuple[str, ...] = ("codex", "copilot", "claude")

# Home-relative config locations. Project-local configs (Codex's ``.codex/config.toml``
# in a trusted project, Claude Code's project ``.mcp.json``) are deliberately NOT
# scanned: doctor cannot know which project the operator will launch the client from,
# and guessing would produce findings about a config the client may never read.
_CONFIG_RELPATH: dict[str, str] = {
    "codex": ".codex/config.toml",
    "copilot": ".copilot/mcp-config.json",
    "claude": ".claude.json",
}

KIND_PAT_UNRESOLVABLE = "pat-unresolvable"
KIND_STALE_PAT_ENV_NAME = "stale-pat-env-name"
KIND_CONFIG_MISSING = "config-missing"
KIND_CONFIG_UNREADABLE = "config-unreadable"
KIND_SERVER_ABSENT = "server-absent"
KIND_PAT_NOT_REFERENCED = "pat-not-referenced"
KIND_PAT_LITERAL = "pat-literal-in-config"
KIND_OK = "ok"

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_UNAVAILABLE = "unavailable"
SEVERITY_OK = "ok"

# ``Bearer $VAR`` (Copilot) or ``Bearer ${VAR}`` (Claude Code). Anything else in an
# Authorization header is treated as a literal and is NEVER echoed.
_ENV_REF = re.compile(
    r"^Bearer\s+(?:\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|\$(?P<bare>[A-Za-z_][A-Za-z0-9_]*))$"
)

# Durable-delivery advice, shared by the two credential findings so the remediation an
# operator reads is identical wherever it surfaces.
_DURABLE_ADVICE = (
    "a bare `export` is transient and dies with the shell it was typed in — persist it "
    "in the shell rc file that starts your login shells, or have your secret manager "
    "inject it, and never commit the value"
)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def scan_mcp_clients(
    *, home: Path | None = None, env: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """Diagnose every supported MCP client's rebar credential wiring.

    ``home`` defaults to the real home directory and ``env`` to :data:`os.environ`; both
    are injectable so the scan can be exercised against a fixture tree and a synthetic
    environment. Returns a flat list of finding dicts, each carrying at least
    ``client``, ``kind`` and ``detail`` (plus ``severity`` and, where known, ``path``).
    Never raises for a missing, unreadable or malformed config.
    """
    base = Path.home() if home is None else Path(home)
    environ: Mapping[str, str] = os.environ if env is None else env
    findings: list[dict[str, Any]] = []
    for client in CLIENT_ORDER:
        findings.extend(_scan_client(client, base, environ))
    return findings


def has_blocking_mcp_client(findings: Iterable[Mapping[str, Any]]) -> bool:
    """True when any client finding is an error.

    ``doctor`` reports these findings but does NOT fold them into its exit code: they are
    read from the operator's HOME rather than from the store, so gating store health on
    them would make the exit depend on whichever client configs sit on the box. This
    predicate is the seam for a caller that DOES want to gate on client wiring.
    """
    return any(f.get("severity") == SEVERITY_ERROR for f in findings)


def render_text(findings: Iterable[Mapping[str, Any]]) -> list[str]:
    """Render the client section as text lines (the caller prints them).

    Every client gets at least one line, healthy ones included: "this client is wired
    correctly" is the answer an operator most often needs, and omitting it would leave
    them unable to tell a healthy box from a check that did not run. Lines carry env-var
    NAMES only — never a resolved value.
    """
    lines = ["doctor: mcp clients"]
    lines.extend(
        f"  {f.get('client', '?')} [{f.get('severity', '?')}] {f.get('kind', '?')}: "
        f"{f.get('detail', '')}"
        for f in findings
    )
    return lines


# ---------------------------------------------------------------------------
# Finding builder
# ---------------------------------------------------------------------------


def _finding(client: str, severity: str, kind: str, detail: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "client": client,
        "severity": severity,
        "kind": kind,
        "detail": detail,
    }
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# Per-client scan
# ---------------------------------------------------------------------------


def _scan_client(client: str, home: Path, env: Mapping[str, str]) -> list[dict[str, Any]]:
    """Scan one client end to end, degrading every fault into a finding."""
    canonical = CANONICAL_PAT_ENV[client]
    path = home / _CONFIG_RELPATH[client]
    if not path.is_file():
        return [
            _finding(
                client,
                SEVERITY_UNAVAILABLE,
                KIND_CONFIG_MISSING,
                f"no MCP client config at {path}; {client} is not configured for rebar on "
                "this machine (a project-local config, if you use one, is not scanned)",
                path=str(path),
            )
        ]
    data, error = _load_config(client, path)
    if error is not None:
        return [
            _finding(
                client,
                SEVERITY_ERROR,
                KIND_CONFIG_UNREADABLE,
                f"could not parse the {client} MCP config at {path}: {error}",
                path=str(path),
            )
        ]
    name, problem = _referenced_env_var(client, data)
    if problem is not None or name is None:
        return [_problem_finding(client, problem or KIND_PAT_NOT_REFERENCED, path, canonical)]
    return _credential_findings(client, name, canonical, path, env)


def _load_config(client: str, path: Path) -> tuple[Any, str | None]:
    """Parse a client config, returning ``(data, error_message)``.

    Codex's config is TOML; the other two are JSON. Any read or parse fault is returned
    as a message rather than raised, so one broken config cannot abort the whole scan.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    try:
        if client == "codex":
            return tomllib.loads(raw.decode("utf-8")), None
        return json.loads(raw.decode("utf-8")), None
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _referenced_env_var(client: str, data: Any) -> tuple[str | None, str | None]:
    """Extract the bearer env-var NAME the config references.

    Returns ``(name, None)`` on success or ``(None, problem_kind)`` where the config does
    not reference a variable at all. The Authorization header's text is inspected but
    never returned, so a literal credential in a config can be reported without echoing
    it.
    """
    entry = _server_entry(client, data)
    if entry is None:
        return None, KIND_SERVER_ABSENT
    if client == "codex":
        name = entry.get("bearer_token_env_var")
        if isinstance(name, str) and name.strip():
            return name.strip(), None
        return None, KIND_PAT_NOT_REFERENCED
    headers = entry.get("headers")
    auth = headers.get("Authorization") if isinstance(headers, dict) else None
    if not isinstance(auth, str) or not auth.strip():
        return None, KIND_PAT_NOT_REFERENCED
    match = _ENV_REF.match(auth.strip())
    if match is None:
        return None, KIND_PAT_LITERAL
    return (match.group("braced") or match.group("bare")), None


def _server_entry(client: str, data: Any) -> dict[str, Any] | None:
    """The ``rebar`` server table for this client, or ``None`` when it is absent."""
    if not isinstance(data, dict):
        return None
    key = "mcp_servers" if client == "codex" else "mcpServers"
    servers = data.get(key)
    if not isinstance(servers, dict):
        return None
    entry = servers.get(SERVER_NAME)
    return entry if isinstance(entry, dict) else None


_PROBLEM_SEVERITY: dict[str, str] = {
    KIND_SERVER_ABSENT: SEVERITY_WARNING,
    KIND_PAT_NOT_REFERENCED: SEVERITY_ERROR,
    KIND_PAT_LITERAL: SEVERITY_ERROR,
}


def _problem_detail(kind: str, client: str, path: Path, canonical: str) -> str:
    if kind == KIND_SERVER_ABSENT:
        return (
            f"the {client} MCP config at {path} declares no {SERVER_NAME!r} server, so "
            f"{client} will not reach the rebar MCP endpoint"
        )
    if kind == KIND_PAT_LITERAL:
        return (
            f"the {SERVER_NAME!r} Authorization header in the {client} config at {path} "
            "does not reference an environment variable in a form this check recognizes "
            f"(`Bearer ${canonical}` or `Bearer ${{{canonical}}}`), so the credential "
            "cannot be verified and may be embedded in the file; point it at "
            f"{canonical} instead, and rotate the value if it was written there"
        )
    return (
        f"the {SERVER_NAME!r} entry in the {client} config at {path} declares no bearer "
        f"credential; it must read the PAT from {canonical}"
    )


def _problem_finding(client: str, kind: str, path: Path, canonical: str) -> dict[str, Any]:
    return _finding(
        client,
        _PROBLEM_SEVERITY[kind],
        kind,
        _problem_detail(kind, client, path, canonical),
        path=str(path),
    )


def _credential_findings(
    client: str,
    name: str,
    canonical: str,
    path: Path,
    env: Mapping[str, str],
) -> list[dict[str, Any]]:
    """The two headline checks, reported INDEPENDENTLY.

    A config can name the wrong variable and still resolve it (the operator exported the
    stale name), and it can name the right variable and fail to resolve it. Reporting
    only one would leave the other cause of an omitted server unaddressed.
    """
    findings: list[dict[str, Any]] = []
    if name != canonical:
        findings.append(
            _finding(
                client,
                SEVERITY_ERROR,
                KIND_STALE_PAT_ENV_NAME,
                f"the {client} MCP config at {path} reads the rebar bearer PAT from "
                f"{name}, which is not this project's canonical variable for {client}; "
                f"migrate the config and your environment to {canonical}",
                path=str(path),
                env_var=name,
                canonical_env_var=canonical,
            )
        )
    # Resolution is a TRUTHINESS TEST ONLY — the value is never bound or formatted.
    if not str(env.get(name) or "").strip():
        findings.append(
            _finding(
                client,
                SEVERITY_ERROR,
                KIND_PAT_UNRESOLVABLE,
                f"the {client} MCP config at {path} reads the rebar bearer PAT from "
                f"{name}, but {name} is unset or empty in this environment, so {client} "
                f"will authenticate with nothing and drop the {SERVER_NAME!r} server; "
                f"{_DURABLE_ADVICE}",
                path=str(path),
                env_var=name,
            )
        )
    if not findings:
        findings.append(
            _finding(
                client,
                SEVERITY_OK,
                KIND_OK,
                f"the {client} MCP config at {path} reads the rebar bearer PAT from "
                f"{canonical}, which is set in this environment",
                path=str(path),
                env_var=canonical,
            )
        )
    return findings
