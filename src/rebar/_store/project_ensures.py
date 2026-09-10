"""Migrate legacy stores into many-to-many project metadata.

The ensure units create the committed projects mapping and capability stamp without ticket
events. :func:`run_ensures` holds the store lock. Each unit checks current state, writes only
drift, and returns ``"ok"`` or ``"changed"``. The seed records the configured Jira project as
the legacy default. The level-triggered stamp activates after the mapping contains multiple
projects.
"""

from __future__ import annotations

import json
import os
import subprocess

from rebar._store import compat, fsutil
from rebar._store.ensures import EnsureOutcome

# The stable, immutable ids of the two units. Persisted in ``.ensure-applied`` and
# asserted against ``ensures._registry()`` by the registry-drift guard, so they must
# never be renamed or repurposed.
SEED_ID = "projects-seed"
STAMP_ID = "projects-compat-stamp"

# The capability token the multi-project mapping requires of a binary. Registered in
# :data:`rebar._store.compat.KNOWN_CAPABILITIES`; an older binary that does not list
# it fails closed on a store that declares it (the expand/contract forward guard).
_MULTI_PROJECT_CAPABILITY = "multi-project-bridge"

# The committed tickets-branch path of the projects mapping, relative to the tracker
# root. A store with no configured Jira project records an EMPTY legacy_default — there
# is NO implicit ``DIG`` fallback (AC2): the implicit default is gone, so only an
# operator who EXPLICITLY configures ``jira.project`` seeds a non-empty mapping.
_PROJECTS_REL_PATH = os.path.join(".bridge_state", "projects.json")


# raw-git-ok: ensure-registry store-maintenance seam (init/fsck), not a ticket event
def _git(tracker: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``git -C <tracker> <args>`` capturing text output (mirrors the raw-git
    helper the init ensure units use). Never raises on a non-zero status — callers
    inspect ``returncode`` — so a tree-check miss is data, not an exception."""
    return subprocess.run(
        ["git", "-C", tracker, *args], capture_output=True, text=True, check=False
    )


def _effective_project() -> str:
    """The store's effective backend project: the configured ``jira.project`` verbatim,
    or the EMPTY string when it is unset. There is NO implicit ``DIG`` fallback (AC2) —
    this mirrors ``Backend.project``, which likewise no longer applies a create-time
    default. Computed with a lazy :func:`rebar.config.compose_config` so this leaf unit does
    not pull config into a hot import path."""
    from rebar.config import compose_config

    return compose_config().jira.project or ""


# raw-git-ok: store-maintenance command, seam-internal
def seed_projects_mapping_unit(tracker: str) -> EnsureOutcome:
    """Seed the committed ``.bridge_state/projects.json`` mapping when absent.

    A committed-tree check makes repeated sweeps no-ops. The deterministic record uses the
    configured ``jira.project`` or an empty legacy default, never an implicit ``DIG`` value.
    Its ``projects`` mapping starts empty so the project command retains ownership. Every init
    runs the seed before ticket state can diverge across clones. The unit commits this file and
    writes no ticket events. Its empty project set cannot trigger the multi-project stamp."""
    if _git(tracker, "show", f"tickets:{_PROJECTS_REL_PATH}").returncode == 0:
        return EnsureOutcome(SEED_ID, "ok", "projects.json present")

    project = _effective_project()
    record = {
        "version": 1,
        "legacy_default": project,
        "projects": {},
    }
    path = os.path.join(tracker, ".bridge_state", "projects.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
    _git(tracker, "add", _PROJECTS_REL_PATH)
    _git(
        tracker,
        "commit",
        "-q",
        "--no-verify",
        "-m",
        "chore: seed .bridge_state/projects.json legacy projects mapping (ticket 462d)",
    )
    return EnsureOutcome(SEED_ID, "changed", f"seeded projects.json (legacy_default={project})")


def _read_mapping(tracker: str) -> dict | None:
    """Read the worktree ``.bridge_state/projects.json`` directly (json.load), or
    ``None`` when it is absent/unreadable — Unit B has nothing to converge without a
    mapping."""
    path = os.path.join(tracker, ".bridge_state", "projects.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_compat_record(tracker: str) -> dict:
    """Read the committed ``.store-compat.json`` record for in-place mutation,
    tolerating an absent/corrupt file by returning a fresh skeleton at the current
    format version with empty ``required_capabilities`` (so the stamp is still
    additive). PRESERVES every other key (e.g. ``epoch``) on the happy path."""
    try:
        with open(compat._record_path(tracker), encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        record = None
    if not isinstance(record, dict):
        record = {}
    record.setdefault("format_version", compat.CURRENT_FORMAT_VERSION)
    caps = record.get("required_capabilities")
    if not isinstance(caps, list):
        caps = []
    record["required_capabilities"] = [c for c in caps if isinstance(c, str)]
    return record


# raw-git-ok: store-maintenance command, seam-internal
def converge_multi_project_stamp_unit(tracker: str) -> EnsureOutcome:
    """Converge the ``multi-project-bridge`` capability stamp.

    An absent mapping, at most one project, or an existing token returns ``"ok"`` without a
    commit. More than one project adds the deduplicated token while preserving all compat keys,
    then writes atomically and commits. This level-triggered check depends on mapping state, so
    the next sweep stamps any route to multiple projects and older binaries fail closed."""
    mapping = _read_mapping(tracker)
    if mapping is None:
        return EnsureOutcome(STAMP_ID, "ok", "no projects.json to converge")

    projects = mapping.get("projects", {})
    project_count = len(projects) if isinstance(projects, dict) else 0
    if project_count <= 1:
        return EnsureOutcome(STAMP_ID, "ok", f"single-project mapping ({project_count})")

    record = _read_compat_record(tracker)
    if _MULTI_PROJECT_CAPABILITY in record["required_capabilities"]:
        return EnsureOutcome(STAMP_ID, "ok", "multi-project capability already stamped")

    record["required_capabilities"] = sorted(
        {*record["required_capabilities"], _MULTI_PROJECT_CAPABILITY}
    )
    fsutil.atomic_write(
        compat._record_path(tracker),
        json.dumps(record, indent=2, sort_keys=True) + "\n",
    )
    _git(tracker, "add", compat.COMPAT_FILENAME)
    _git(
        tracker,
        "commit",
        "-q",
        "--no-verify",
        "-m",
        "chore: stamp multi-project-bridge capability on .store-compat.json (ticket 462d)",
    )
    return EnsureOutcome(STAMP_ID, "changed", "stamped multi-project-bridge capability")
