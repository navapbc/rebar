"""Thin CLI adapter for explicit tracker-footprint measurement."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rebar import config
from rebar._store import footprint


def _available_text(field: object) -> str:
    if isinstance(field, dict) and isinstance(field.get("value"), int):
        return str(field["value"])
    if isinstance(field, dict) and isinstance(field.get("unavailable"), dict):
        reason = field["unavailable"].get("reason", "unavailable")
        return f"unavailable ({reason})"
    return "unavailable"


def _render_text(report: dict[str, object]) -> str:
    source = report["source"]
    object_database = report["object_database"]
    layers = report["layers"]
    definitions = report["definitions"]
    assert isinstance(source, dict)
    assert isinstance(object_database, dict)
    assert isinstance(layers, dict)
    assert isinstance(definitions, dict)

    reasons = object_database["shared_reasons"]
    reason_text = f" ({', '.join(reasons)})" if reasons else ""
    lines = [
        f"tracker footprint ({report['mode']})",
        f"  source: {source['requested_ref']} at {source['tip']}",
        f"  measured ref: {source['measured_ref']}",
        f"  object database: {object_database['scope']}{reason_text}",
    ]
    for name in ("pack", "checkout", "git_directory", "whole_clone"):
        layer = layers[name]
        assert isinstance(layer, dict)
        line = f"  {name}: logical_bytes={layer['logical_bytes']} file_count={layer['file_count']}"
        if "allocated_bytes" in layer:
            line += (
                f" allocated_bytes={_available_text(layer['allocated_bytes'])}"
                f" allocation_overhead_bytes="
                f"{_available_text(layer['allocation_overhead_bytes'])}"
            )
        if "scope" in layer:
            line += f" scope={layer['scope']}"
        if "complete" in layer:
            line += f" complete={layer['complete']}"
            if layer["complete"] is False:
                line += " (non-exclusive: objects reside in an unmeasured alternate database)"
        lines.append(line)
    lines.append("  definitions:")
    for name, definition in definitions.items():
        lines.append(f"    {name}: {definition}")
    return "\n".join(lines) + "\n"


def tracker_footprint_cli(argv: list[str], *, repo_root: str | None = None) -> int:
    """Run ``rebar tracker-footprint`` without initializing or changing a store."""

    from rebar._cli._parser import ParseError, render_parse_error
    from rebar._cli._parsers.advanced.tracker_footprint import build

    try:
        args = build(prog="rebar tracker-footprint").parse_args(argv)
    except ParseError as exc:
        return render_parse_error(exc)

    root = Path(config.repo_root(repo_root) if repo_root is not None else config.repo_root())
    try:
        if args.fresh_clone:
            report = footprint.measure_fresh_clone(root)
        else:
            report = footprint.measure_tracker(
                config.tracker_dir(root),
                remote=config.tickets_remote(root),
                branch=config.tickets_branch(root),
                mode="mounted",
            )
    except footprint.FootprintError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1

    if args.output == "json":
        sys.stdout.write(json.dumps(report, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(_render_text(report))
    return 0
