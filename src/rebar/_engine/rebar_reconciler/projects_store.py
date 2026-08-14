"""Per-store "bridge projects" mapping (story c927).

Records which tracker projects a store syncs and which repos each project's
tickets belong to. Persisted as JSON at
``.tickets-tracker/.bridge_state/projects.json`` on the tickets branch,  # tickets-boundary-ok
beside the binding store.

The record shape::

    {"version": 1, "legacy_default": "REB", "projects": {"REB": {"repos": ["rebar"]}}}

The ``projects`` key set IS the store's sync list. ``legacy_default`` is the
project pre-epic tickets (those with no ``bridge_project`` field) resolve to.

Importable standalone: stdlib only, no ``rebar.*`` imports. Takes ``repo_root``
and derives paths directly, mirroring ``binding_store``.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_VERSION = 1


@dataclass
class Mapping:
    """A loaded projects mapping.

    ``legacy_default`` is the project an absent ``bridge_project`` field resolves
    to (``None`` when nothing is seeded — fail-safe). ``projects`` is the
    ``{key: {"repos": [...]}}`` sync list.
    """

    legacy_default: str | None = None
    projects: dict[str, dict[str, Any]] = field(default_factory=dict)


def _projects_path(repo_root: Path) -> Path:
    return repo_root / ".tickets-tracker" / ".bridge_state" / "projects.json"  # tickets-boundary-ok


def load_mapping(repo_root: str | os.PathLike[str]) -> Mapping:
    """Read and validate the ``projects.json`` record under ``repo_root``.

    - ABSENT record → LEGAL: an empty mapping whose ``legacy_default`` is
      ``None`` (nothing syncs until seeded).
    - MALFORMED record (parse error / truncation / bad shape) → FAIL CLOSED:
      raise ``ValueError`` rather than silently degrade to empty.
    """
    path = _projects_path(Path(repo_root))
    if not path.exists():
        return Mapping()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        raise ValueError(
            f"projects.json is corrupt or contains git conflict markers "  # tickets-boundary-ok
            f"and cannot be parsed. File: {path}. Original error: {exc}. "
            f"Recovery: resolve the merge conflict or restore the file from "
            f"the tickets branch."
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"projects.json must be a JSON object. File: {path}."
        )  # tickets-boundary-ok

    projects = data.get("projects", {})
    if not isinstance(projects, dict):
        raise ValueError(
            f"projects.json 'projects' must be an object. File: {path}."  # tickets-boundary-ok
        )
    for key, entry in projects.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("repos"), list):
            raise ValueError(
                f"projects.json entry {key!r} must be {{'repos': [...]}}. "  # tickets-boundary-ok
                f"File: {path}."
            )

    legacy_default = data.get("legacy_default")
    if legacy_default is not None and not isinstance(legacy_default, str):
        raise ValueError(
            f"projects.json 'legacy_default' must be a string or null. "  # tickets-boundary-ok
            f"File: {path}."
        )

    return Mapping(legacy_default=legacy_default or None, projects=projects)


def resolve_inbound_bridge_fields(
    jira_key: str, repo_root: str | os.PathLike[str]
) -> dict[str, Any]:
    """Bridge fields to stamp on an inbound-created ticket (story 1734).

    The source project is the Jira key's prefix (``DIG-123`` → ``DIG``); ``repos``
    are that project's mapped repos (empty when the project is not in the mapping).
    Returns ``{"bridge_project": <prefix>, "repos": [...]}`` — both keys the
    reducer's CREATE processor projects (story cef7).
    """
    source_project = jira_key.rsplit("-", 1)[0]
    repos = load_mapping(repo_root).projects.get(source_project, {}).get("repos", [])
    return {"bridge_project": source_project, "repos": list(repos)}


def resolve_project(ticket: dict[str, Any], mapping: Mapping) -> str | None:
    """Tri-state resolution of a ticket's optional ``bridge_project`` field.

    - field ABSENT → the mapping's ``legacy_default`` (a str, or ``None`` when
      the default is empty/None).
    - ``""`` (present, empty string) → ``None``.
    - non-empty string → that value verbatim (not validated against the set).
    """
    if "bridge_project" not in ticket:
        return mapping.legacy_default or None
    value = ticket.get("bridge_project")
    if not value:
        return None
    return value


# -- mutation helpers (internal; called by the lib/CLI/MCP layers) ---------


def read_projects(repo_root: str | os.PathLike[str]) -> dict[str, dict[str, Any]]:
    """Return the ``{key: {"repos": [...]}}`` projects mapping."""
    return load_mapping(repo_root).projects


def set_project(repo_root: str | os.PathLike[str], key: str, repos: list[str]) -> None:
    """Set ``key``'s repos list (REPLACE semantics) and persist atomically."""
    root = Path(repo_root)
    record = _read_record(root)
    record["projects"][key] = {"repos": list(repos)}
    _write_record(root, record)


def remove_project(repo_root: str | os.PathLike[str], key: str) -> None:
    """Remove ``key`` from the projects mapping.

    Raises ``KeyError`` naming the key if it is not present, so the caller can
    exit non-zero and report it.
    """
    root = Path(repo_root)
    record = _read_record(root)
    if key not in record["projects"]:
        raise KeyError(key)
    del record["projects"][key]
    _write_record(root, record)


def _read_record(repo_root: Path) -> dict[str, Any]:
    """Read the full record (for mutation). Absent → default skeleton."""
    path = _projects_path(repo_root)
    if not path.exists():
        return {"version": _VERSION, "legacy_default": None, "projects": {}}
    # load_mapping validates + fails closed on corruption.
    mapping = load_mapping(repo_root)
    return {
        "version": _VERSION,
        "legacy_default": mapping.legacy_default,
        "projects": mapping.projects,
    }


def _write_record(repo_root: Path, record: dict[str, Any]) -> None:
    """Atomically persist the record (tempfile + os.replace).

    Creates ``.bridge_state`` if missing; never touches sibling files.
    """
    path = _projects_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="projects_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
