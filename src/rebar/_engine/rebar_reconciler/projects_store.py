"""Per-store "bridge projects" mapping (story c927).

Records which tracker projects a store syncs and which repos each project's
tickets belong to. Persisted as JSON at
``<store>/.bridge_state/projects.json`` on the tickets branch, beside the binding
store (``<store>`` is the RESOLVED, relocatable tracker directory).

The record shape::

    {"version": 1, "legacy_default": "REB", "projects": {"REB": {"repos": ["rebar"]}}}

The ``projects`` key set IS the store's sync list. ``legacy_default`` is the
project pre-epic tickets (those with no ``bridge_project`` field) resolve to.

Importable standalone: the READ path keeps its MODULE level stdlib only, taking
``repo_root`` and resolving the store through a function-body ``rebar.config`` import
(an absolute import that resolves even under a by-path spec load), mirroring
``binding_store`` — that is what the engine reconciler's read-only importers rely on.
The WRITE path (``_write_record``, only reached from the ``rebar._lib_ops`` library
layer) lazily imports the shared ``rebar._store`` seams (``fsutil.atomic_write``)
rather than hand-rolling the write.
"""

from __future__ import annotations

import json
import os
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
    """The record path under the RESOLVED store (``REBAR_TRACKER_DIR`` > ``tracker.dir`` >
    the default name under ``repo_root``) — the store is relocatable, so composing the
    default name would read/write a directory the operator never configured."""
    from rebar.config import tracker_dir as _resolve_store

    return _resolve_store(repo_root) / ".bridge_state" / "projects.json"


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

    - field ABSENT, or present as ``None`` (the reducer's seeded "absent/legacy"
      sentinel, cef7 ``_state.py:53-58``) → the mapping's ``legacy_default`` (a
      str, or ``None`` when the default is empty/None).
    - ``""`` (present, empty string) → ``None`` (the deliberate never-sync value).
    - non-empty string → that value verbatim (not validated against the set).

    ``None`` and ``""`` are distinct: the reducer materializes ``bridge_project``
    as ``None`` for every no-flag create, so treating ``None`` as never-sync would
    suppress the entire legacy cohort ``legacy_default`` exists to serve.
    """
    value = ticket.get("bridge_project")
    if value is None:
        return mapping.legacy_default or None
    if value == "":
        return None
    return value


# -- mutation helpers (internal; called by the lib/CLI/MCP layers) ---------


def read_projects(repo_root: str | os.PathLike[str]) -> dict[str, dict[str, Any]]:
    """Return the ``{key: {"repos": [...]}}`` projects mapping."""
    return load_mapping(repo_root).projects


def _require_valid_key(key: str) -> None:
    """Reject a syntactically-invalid project key BEFORE any read/write/publish.

    Reuses ``fetcher._validate_project_key`` — the single source of the key grammar
    (``_PROJECT_KEY_RE``) — so this is SYNTACTIC only (no duplicated regex, and no
    network/Jira call). Raising here, before ``_read_record``/``_write_record``,
    guarantees a malformed key never mutates or publishes the mapping. The fetcher
    lives beside this module in ``rebar_reconciler``; the import is lazy to keep the
    read path stdlib-only (mirroring ``_write_record``'s lazy seam import).
    """
    from rebar_reconciler.fetcher import _PROJECT_KEY_RE, _validate_project_key

    try:
        _validate_project_key(key)
    except ValueError as exc:
        raise ValueError(
            f"invalid bridge project key {key!r}: a Jira project key must match "
            f"{_PROJECT_KEY_RE.pattern} (a letter followed by letters, digits, or "
            f"underscores). Refusing to write the projects mapping."
        ) from exc


def set_project(repo_root: str | os.PathLike[str], key: str, repos: list[str]) -> None:
    """Set ``key``'s repos list (REPLACE semantics) and persist atomically.

    The key is validated syntactically first (``_require_valid_key``); an invalid key
    raises ``ValueError`` before any file write or publish.
    """
    _require_valid_key(key)
    root = Path(repo_root)
    record = _read_record(root)
    record["projects"][key] = {"repos": list(repos)}
    _write_record(root, record)


def remove_project(repo_root: str | os.PathLike[str], key: str) -> None:
    """Remove ``key`` from the projects mapping.

    The key is validated syntactically first (``_require_valid_key``); an invalid key
    raises ``ValueError`` before any mutation. Raises ``KeyError`` naming the key if it
    is (validly) not present, so the caller can exit non-zero and report it.
    """
    _require_valid_key(key)
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
    """Atomically persist the record via the shared ``fsutil.atomic_write`` seam.

    Creates ``.bridge_state`` if missing (``atomic_write`` requires the parent dir to
    already exist); never touches sibling files. The serialized bytes are exactly
    ``json.dumps(record, indent=2, sort_keys=True)`` plus a single trailing newline —
    byte-identical to the previous hand-rolled write.
    """
    from rebar._store import fsutil

    path = _projects_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fsutil.atomic_write(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
