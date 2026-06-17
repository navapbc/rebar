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


def config_cli(argv: list[str]) -> int:
    """``rebar config`` entrypoint. ``--output text`` (default) or ``json``;
    ``--root`` overrides the repo root used for project-config discovery."""
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
