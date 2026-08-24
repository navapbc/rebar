#!/usr/bin/env python3
"""Keep the environment contract in ``server.json`` aligned with code.

MCP clients read ``server.json`` before installation. The
``rebar.mcp_server.MCP_ENV_VARS`` inventory defines the supported names and
descriptions. The manifest contract marks every declared variable as optional,
so each canonical ``isRequired`` value is ``false``.

This checker compares complete records and rejects missing names, extra names,
changed fields, and duplicate names.

Regeneration command

    python - <<'PY'
    import json, rebar.mcp_server as m
    d = json.load(open("server.json"))
    d["packages"][0]["environmentVariables"] = [
        {"name": v["name"], "description": v["description"], "isRequired": False}
        for v in m.MCP_ENV_VARS
    ]
    json.dump(d, open("server.json", "w"), indent=2, ensure_ascii=False)
    open("server.json", "a").write("\n")
    PY

and commit the result.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

from rebar.mcp_server import MCP_ENV_VARS

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_JSON = REPO_ROOT / "server.json"
RECORD_FIELDS = ("name", "description", "isRequired")
EnvRecord = dict[str, object]


def canonical_env_records() -> list[EnvRecord]:
    """Build the complete manifest records defined by the code inventory."""
    return [
        {
            "name": item["name"],
            "description": item["description"],
            "isRequired": False,
        }
        for item in MCP_ENV_VARS
    ]


def manifest_env_records() -> list[EnvRecord]:
    """Read the environment records advertised by the first package."""
    data = cast(dict[str, object], json.loads(SERVER_JSON.read_text()))
    packages = data.get("packages")
    if not isinstance(packages, list) or not packages:
        raise SystemExit("server.json: no packages entry contains environmentVariables")
    package = packages[0]
    if not isinstance(package, dict):
        raise SystemExit("server.json: the first package must be an object")
    records = package.get("environmentVariables", [])
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise SystemExit("server.json: environmentVariables must be a list of objects")
    return [cast(EnvRecord, item) for item in records]


def _record_name(record: EnvRecord) -> str:
    name = record.get("name")
    return name if isinstance(name, str) else repr(name)


def _duplicate_names(records: list[EnvRecord]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for record in records:
        name = _record_name(record)
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    return sorted(duplicates)


def _same_value(expected: object, advertised: object) -> bool:
    return type(expected) is type(advertised) and expected == advertised


def compare_env_records(canonical: list[EnvRecord], advertised: list[EnvRecord]) -> list[str]:
    """Return diagnostics for every difference between two record inventories."""
    diagnostics: list[str] = []
    canonical_duplicates = _duplicate_names(canonical)
    advertised_duplicates = _duplicate_names(advertised)
    if canonical_duplicates:
        diagnostics.append(f"DUPLICATE names in MCP_ENV_VARS: {canonical_duplicates}")
    if advertised_duplicates:
        diagnostics.append(f"DUPLICATE names in server.json: {advertised_duplicates}")

    canonical_by_name = {_record_name(item): item for item in canonical}
    advertised_by_name = {_record_name(item): item for item in advertised}
    canonical_names = set(canonical_by_name)
    advertised_names = set(advertised_by_name)

    missing = sorted(canonical_names - advertised_names)
    extra = sorted(advertised_names - canonical_names)
    if missing:
        diagnostics.append(f"MISSING from server.json: {missing}")
    if extra:
        diagnostics.append(f"EXTRA in server.json: {extra}")

    for name in sorted(canonical_names & advertised_names):
        expected = canonical_by_name[name]
        found = advertised_by_name[name]
        for field in RECORD_FIELDS:
            if _same_value(expected.get(field), found.get(field)):
                continue
            diagnostics.append(
                f"CHANGED {name}.{field}. "
                f"Canonical value {expected.get(field)!r}. "
                f"server.json value {found.get(field)!r}."
            )
    return diagnostics


def main() -> int:
    canonical = canonical_env_records()
    advertised = manifest_env_records()
    diagnostics = compare_env_records(canonical, advertised)

    if diagnostics:
        print("::error::server.json environment contract differs from MCP_ENV_VARS")
        for diagnostic in diagnostics:
            print(f"  {diagnostic}")
        print("  Regenerate server.json environmentVariables from MCP_ENV_VARS.")
        print("  The script docstring contains the regeneration command.")
        return 1

    print(f"server.json environment contract: OK. {len(canonical)} records match MCP_ENV_VARS.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
