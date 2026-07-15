"""``rebar config`` — resolved-configuration transparency command.

Prints every typed-config key with its resolved value AND the precedence layer
that value came from (``cli`` > ``env`` > ``project`` > ``user`` > ``default``),
the ruff ``--show-settings`` / ``pip config debug`` pattern. This is the read side
of the config-refinement work (epic a621): a single place to answer "what value is
rebar actually using for X, and why?" without guessing at the layering.

It resolves through :func:`rebar.config.resolve_with_sources` — the SAME layer
assembly :func:`rebar.config.load_config` uses — so the reported provenance can
never disagree with the value the live load produces. Output is portable: only the
discovered config-file *paths* are machine-specific (and are reported as such), the
values themselves carry no host state.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from typing import Any

from rebar import config as _config
from rebar.config import _SECTIONS  # the canonical (section, key) inventory


def _fmt_value(value: Any) -> str:
    """Render a resolved value for the text table: TOML-ish booleans, quoted empty
    strings (so an unset string is visibly empty, not a blank gap)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value == "":
        return '""'
    return str(value)


def _resolved(root: str | None) -> dict[str, Any]:
    """The full transparency payload: nested values, per-key source layer, and the
    discovered project / user config locations. Shared by both output modes so the
    JSON and text views are guaranteed to describe the same resolution."""
    cfg, sources, project = _config.resolve_with_sources(root)
    up = _config.user_config_path()
    return {
        "config": dataclasses.asdict(cfg),
        "sources": sources,
        "project_config": (
            None if project is None else {"path": str(project[0]), "kind": project[1]}
        ),
        "user_config": {"path": str(up), "exists": up.is_file()},
        "precedence": list(_config.LAYER_ORDER),
    }


def _render_text(payload: dict[str, Any]) -> str:
    """A grouped ``section.key = value    [source]`` table, aligned, with a header
    naming the config files that fed the resolution."""
    cfg, sources = payload["config"], payload["sources"]
    proj, user = payload["project_config"], payload["user_config"]
    lines: list[str] = []
    lines.append("# rebar resolved configuration")
    lines.append(
        "# project config: " + (f"{proj['path']} ({proj['kind']})" if proj else "(none found)")
    )
    lines.append(
        "# user config:    " + (user["path"] if user["exists"] else f"{user['path']} (none)")
    )
    lines.append("# precedence:     " + " < ".join(payload["precedence"]))
    lines.append("")
    # Pre-render every row so BOTH the key and value columns can be width-aligned to
    # their widest actual entry (a hardcoded value width misaligns on long values
    # like jira.url / scratch.base_dir).
    rows = [
        (f"{sect}.{key}", _fmt_value(cfg[sect][key]), sources[sect][key])
        for sect in _SECTIONS
        for key in _SECTIONS[sect]
    ]
    kw = max((len(dotted) for dotted, _, _ in rows), default=0)
    vw = max((len(value) for _, value, _ in rows), default=0)
    for dotted, value, src in rows:
        lines.append(f"{dotted:<{kw}} = {value:<{vw}} [{src}]")
    return "\n".join(lines) + "\n"


def _raw_toml_tables(root: str | None) -> dict:
    """The RAW (uncoerced) merged nested TOML tables (user then project, project wins),
    ``{section: {key: value}}`` — read directly, NOT through the coercing/raising
    ``load_config`` path, so ``config validate`` can inspect keys (incl. tombstoned ones
    that ``coerce_sparse`` would drop) without aborting on the first removed input."""
    merged: dict[str, dict] = {}

    def _merge(table: dict) -> None:
        for sect, val in table.items():
            if isinstance(val, dict):
                merged.setdefault(sect, {}).update(val)

    try:
        up = _config.user_config_path()
        if up.is_file():
            _merge(_config._read_toml_table(up, pyproject=False))
        proj = _config._discover_project_config(root)
        if proj is not None:
            path, kind = proj
            _merge(_config._read_toml_table(path, pyproject=(kind == "pyproject")))
    except _config.ConfigError:
        # An unreadable/malformed config file: validate should still report the env +
        # file tombstones it can, so treat the TOML side as empty rather than aborting.
        return merged
    return merged


def _validate(root: str | None) -> int:
    """``rebar config validate``: scan the live env + parsed TOML + the legacy config
    file for REMOVED inputs, report every match, and exit non-zero iff any error-class
    input is present (a clean environment exits 0). Non-raising — reports the whole
    migration surface at once rather than aborting on the first removed input."""
    import os

    from rebar._deprecations import scan_tombstones

    toml_tables = _raw_toml_tables(root)
    legacy = _config.repo_root(root) / ".rebar" / "config.conf"
    file_paths = [".rebar/config.conf"] if legacy.is_file() else []

    hits = scan_tombstones(env=dict(os.environ), toml_tables=toml_tables, file_paths=file_paths)
    if not hits:
        print("rebar config validate: OK — no removed inputs are set.")
        return 0

    has_error = False
    for ri, ctx in hits:
        has_error = has_error or ri.behavior == "error"
        level = "ERROR" if ri.behavior == "error" else "WARN"
        repl = ri.replacement if ri.replacement else "(no replacement)"
        print(f"{level}: {ri.kind} {ctx} was removed in {ri.removed_in} — use {repl} instead")
    if has_error:
        sys.stderr.write(
            "rebar config validate: one or more REMOVED inputs are still set "
            "(see above); migrate them.\n"
        )
        return 1
    return 0


def config_cli(argv: list[str]) -> int:
    """``rebar config`` entrypoint. Subcommand ``validate`` scans for removed inputs;
    otherwise shows resolved config. ``--output text`` (default) or ``json``;
    ``--root`` overrides the repo root used for project-config discovery."""
    if argv and argv[0] == "validate":
        vparser = argparse.ArgumentParser(
            prog="rebar config validate",
            description="Scan the environment + config for REMOVED (tombstoned) inputs. "
            "Exits non-zero if any load-bearing removed input is still set.",
        )
        vparser.add_argument(
            "--root", default=None, help="repo root for config discovery (default: auto)"
        )
        vargs = vparser.parse_args(argv[1:])
        return _validate(vargs.root)

    parser = argparse.ArgumentParser(
        prog="rebar config",
        description="Show the resolved rebar configuration and the precedence layer "
        "(cli > env > project > user > default) each value came from.",
    )
    parser.add_argument("--output", "-o", choices=["text", "json"], default="text")
    parser.add_argument(
        "--root", default=None, help="repo root for project-config discovery (default: auto)"
    )
    args = parser.parse_args(argv)

    try:
        payload = _resolved(args.root)
    except _config.ConfigError as exc:
        # A malformed config or a strict-mode unknown key (REBAR_CONFIG_UNKNOWN_KEYS
        # =error) is a user-facing condition — report it cleanly, not as a traceback.
        sys.stderr.write(f"rebar config: {exc}\n")
        return 1
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_render_text(payload), end="")
    return 0
