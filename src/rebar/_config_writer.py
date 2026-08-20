"""The config WRITE path — rebar's only writer of a rebar-owned ``rebar.toml``.

Split out of ``rebar.config`` (rebar-ticket a66c-9329-e9c9-4aec), which had reached the
locked module-size cap with no headroom left. ``config.py`` is otherwise entirely a
READ/RESOLVE module — repo-root and config-file discovery, the layered precedence stack,
the resolution caches, and the owned composition-root accessors. Exactly two symbols write
a file, and they form a closed call cluster on a seam the call graph already had:

  * :func:`_emit_toml` — the small, self-contained TOML emitter, whose ONLY caller is
  * :func:`write_jira_config` — the read-whole / mutate / re-emit / atomic-replace writer
    for the ``[jira]`` table of a rebar-owned ``rebar.toml``.

Nothing in the read path calls either one, and the pair only calls names ``config.py``
already imported from its siblings — so the cluster lifts out whole. The design of record
for this write path is ADR 0070.

``rebar.config`` RE-EXPORTS both names, including the private ``_emit_toml``: the module is
public surface and ``from rebar.config import X`` (and the module-attribute form
``_config.write_jira_config`` used by ``rebar._cli._jira_onboard``) must keep working.
``tests/unit/test_config_writer_surface.py`` pins that contract.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from rebar._config_schema import ConfigError


def _emit_toml(data: dict) -> str:
    """Serialize a nested config mapping back to TOML text.

    A small, self-contained emitter covering the scalar value types a rebar config
    file legitimately holds — ``bool`` / ``int`` / ``float`` / ``str`` and a flat
    ``list`` of those — as top-level keys, then one ``[section]`` table per nested
    dict. It is deliberately NOT a general TOML writer (no inline tables, no nested
    tables, no datetimes): it exists only so :func:`write_jira_config` can round-trip
    a *rebar-owned* ``rebar.toml`` (read whole with stdlib ``tomllib`` → mutate the
    dict → re-emit), sidestepping any surgical text-splicing.

    **Fail-closed on an unsupported value type.** A full read-mutate-emit cycle would
    otherwise silently corrupt a value the emitter does not model (e.g. a datetime, a
    nested sub-table, or an array-of-tables). Rather than mis-emit, an unsupported
    type raises :class:`ConfigError` — the caller aborts WITHOUT writing, so an
    existing file is never clobbered. ``bool`` is checked before ``int`` (Python
    ``bool`` is an ``int`` subclass). Floats are emitted via ``repr`` so the value
    round-trips. Section/key order is preserved as given; empty tables are skipped;
    comments are not preserved (acceptable on a rebar-owned file — we never re-emit a
    user ``pyproject.toml``)."""

    def _scalar(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return repr(value)
        if isinstance(value, str):
            s = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{s}"'
        raise ConfigError(
            f"cannot serialize config value of type {type(value).__name__!r} "
            f"({value!r}); rebar's config writer only supports scalars and flat lists"
        )

    def _value(value: Any) -> str:
        if isinstance(value, list):
            return "[" + ", ".join(_scalar(v) for v in value) + "]"
        return _scalar(value)

    top = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    lines: list[str] = []
    for key, value in top.items():
        lines.append(f"{key} = {_value(value)}")
    for name, table in tables.items():
        if not table:  # never emit an empty [section] header
            continue
        if lines:
            lines.append("")
        lines.append(f"[{name}]")
        for key, value in table.items():
            if isinstance(value, dict):
                raise ConfigError(
                    f"cannot serialize nested sub-table [{name}.{key}]; rebar's config "
                    "writer supports only top-level keys and one level of [section]"
                )
            lines.append(f"{key} = {_value(value)}")
    return ("\n".join(lines) + "\n") if lines else ""


def write_jira_config(
    url: str = "",
    user: str = "",
    project: str = "",
    *,
    root: str | os.PathLike[str] | None = None,
    clear: bool = False,
) -> Path:
    """Persist the non-secret Jira settings (``url`` / ``user`` / ``project``) to a
    rebar-owned ``rebar.toml`` ``[jira]`` section and return the file written.

    The SECRET ``JIRA_API_TOKEN`` is NEVER a config key and is never written here —
    only the three connection coordinates are. This is the write counterpart to the
    read path in :func:`resolve_jira_settings` / :func:`load_config`.

    Target selection (deterministic): :func:`_discover_project_config` →

    * a ``rebar.toml`` → that file is the target.
    * a ``pyproject.toml`` / nothing → the target is ``<repo_root>/rebar.toml``
      (CREATED if absent). A user-owned ``pyproject.toml`` is NEVER edited; the fresh
      ``rebar.toml`` wins read precedence over pyproject (rebar.toml is probed first by
      the discovery walk).

    Mechanism: read the target whole with stdlib ``tomllib`` (so ``[jira]`` /
    ``jira = {…}`` inline-table / ``jira.url`` dotted-key forms all normalize to the
    same nested dict — there is no form-specific code and no way to append a
    duplicate section), mutate the in-memory ``jira`` table, and re-emit the whole
    file via :func:`_emit_toml`. No text-region splicing, so no section-end-boundary
    detection is needed. The write is atomic (temp file in the same directory +
    ``os.replace``); a single ``write`` cannot leave a torn/partial file. The
    read-modify-write is last-writer-wins across concurrent writers — fine for an
    interactive single-operator onboarding tool.

    With ``clear=True`` the three keys are removed (and an emptied ``jira`` table
    dropped) rather than set — the ``--reset`` path.

    Raises :class:`ConfigError` if an existing target is unreadable/malformed TOML
    (fail-closed: nothing is written) or the write itself fails. Raises
    :class:`InsecureUrlError` (a ``ConfigError`` subclass) if ``url`` is a non-https
    scheme, before writing anything — the wizard never persists a cleartext url (bug
    bdb8)."""
    # Resolve repo-root and project-config DISCOVERY through the composition root
    # (``rebar.config``) rather than binding ``rebar._config_sources`` directly here. Two
    # reasons, and the first is load-bearing:
    #
    #  * These names were looked up in ``rebar.config``'s namespace at CALL time while this
    #    function lived there, and callers patch them AS MODULE ATTRIBUTES on that module —
    #    ``tests/interfaces/facades/test_bridge_vocabulary_heldout.py`` does exactly that
    #    (``monkeypatch.setattr(config, "repo_root", …)``) to redirect the write into a tmp
    #    root. Importing the siblings straight into THIS module would rebind them at import
    #    time, silently ignore that patch, and write outside the intended root.
    #  * ``config.py`` is the composition root that owns config discovery; a below-seam
    #    module RECEIVING that composition (rather than re-deriving it) is the same rule
    #    ``compose_config`` / ``mcp_gate`` state for every other consumer.
    #
    # The import is function-local because ``rebar.config`` imports THIS module at its top
    # to re-export the write path — a module-level import here would be a cycle.
    from rebar import config as _config

    base = _config.repo_root(root)
    proj = _config._discover_project_config(root)
    if proj is not None and proj[1] == "toml":
        target = proj[0]
    else:
        target = base / "rebar.toml"

    data: dict[str, Any] = {}
    if target.is_file():
        try:
            data = _config._parse_toml(target)
        except ConfigError:
            raise  # malformed existing rebar.toml → fail closed, no write
    # tomllib returns a plain dict; ensure the jira table exists as a mutable dict.
    jira = data.get("jira")
    if not isinstance(jira, dict):
        jira = {}
    if clear:
        for k in ("url", "user", "project"):
            jira.pop(k, None)
    else:
        # Fail-closed on a cleartext url BEFORE writing, so the onboarding wizard can never
        # silently persist a credential-exposing http:// url (bug bdb8). https-only: an
        # http loopback needs a hand-written [jira] allow_insecure = true.
        from rebar._config_schema import _validate_https_url

        _validate_https_url(
            url, allow_insecure=False, url_label="jira.url", override_label="jira.allow_insecure"
        )
        jira["url"] = url
        jira["user"] = user
        jira["project"] = project
    if jira:
        data["jira"] = jira
    else:
        data.pop("jira", None)

    text = _emit_toml(data)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        raise ConfigError(f"could not write config {target}: {exc}") from None
    return target
