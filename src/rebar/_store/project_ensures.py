"""Legacy-store → many-to-many projects-model ensure units (ticket 462d).

The many-to-many projects model (story c927) records which tracker projects a
store syncs in a committed ``.bridge_state/projects.json`` mapping (see
:mod:`rebar._engine.rebar_reconciler.projects_store`). A store initialized before
that feature has NO such mapping, so every absent-``bridge_project`` ticket has
nothing to resolve to. These two ensure-registry units migrate such a legacy
store WITHOUT writing any per-ticket events — they only stamp two committed
tickets-branch files — so the migration is invisible to the event log and cheap
to re-run.

Both units follow the School-B check-then-act shape of the built-in units in
:mod:`rebar._commands.init` (``_gitignore_unit`` / ``_store_compat_unit``): they
inspect current state via a git tree-check or a direct file read BEFORE writing,
act only on drift, and return an :class:`~rebar._store.ensures.EnsureOutcome`
whose status is ``"changed"`` (drift corrected) or ``"ok"`` (already converged,
zero git commits). :func:`run_ensures` runs both under the store write lock.

Unit A (:func:`seed_projects_mapping_unit`) seeds the one-project mapping from the
store's configured Jira project. Unit B (:func:`converge_multi_project_stamp_unit`)
is a level-triggered backstop that stamps the ``multi-project-bridge`` capability
into ``.store-compat.json`` once the mapping actually holds more than one project —
it is keyed on the mapping's state, NOT on how the store became multi-project.
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
    return subprocess.run(["git", "-C", tracker, *args], capture_output=True, text=True)


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
    """Seed the committed ``.bridge_state/projects.json`` mapping (ensure-registry
    Unit A, ticket 462d).

    Tree-checks the committed blob first (like :func:`init._gitignore_unit`): if the
    mapping is already committed the store is converged, so this writes NOTHING and
    returns ``"ok"`` (a re-sweep on a seeded store makes zero git commits). Otherwise
    it computes the store's effective backend project (``jira.project`` verbatim, or an
    EMPTY string when unset — no ``DIG`` fallback) and writes a mapping recording ONLY
    that ``legacy_default`` with an EMPTY
    ``projects`` set — in the schema
    :mod:`rebar._engine.rebar_reconciler.projects_store` validates. The blob is a
    COMMITTED tickets-branch file (not gitignored), so the unit ``git add``s + commits
    it itself; the write is deterministic (``sort_keys`` + trailing newline) so a
    re-seed of an identical store produces byte-identical content.

    The seed runs UNCONDITIONALLY at every init (not gated on the store already
    holding tickets): that is what keeps it *convergent* across clones. Each clone's
    first init writes the identical mapping before any ticket exists and commits it
    into the shared tickets-branch base, so a peer that fetches that base finds the
    blob present and the tree-check short-circuits to ``"ok"`` — no divergent per-clone
    commit, so ``git merge --ff-only`` still converges (gating the seed on has-tickets
    instead makes one clone seed while another skips, forking the branch). It seeds an
    EMPTY projects set (not a one-project entry) so the project set stays owned by the
    ``bridge projects`` command (story c927): ``bridge projects list`` on a fresh store
    is empty until the operator ``set``s a key, and the ``legacy_default`` is a sibling
    field the resolver uses to map every absent-``bridge_project`` ticket. The record
    always has ≤1 projects, so the multi-project stamp (Unit B) never fires from it.

    This is the ONLY trigger for the migration and it writes no per-ticket events — a
    store gains its mapping purely by stamping this one file."""
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
    """Converge the ``multi-project-bridge`` capability stamp on ``.store-compat.json``
    (ensure-registry Unit B, ticket 462d) — a level-triggered BACKSTOP, not the
    migration trigger.

    Reads the mapping state directly and stamps the capability into the compat
    record's ``required_capabilities`` ONLY when the mapping holds MORE THAN ONE
    project and the token is not already present. A zero- or one-project mapping is
    never stamped (returns ``"ok"``), an absent mapping is a no-op (``"ok"``), and an
    already-stamped record is a no-op (``"ok"``) — so this makes zero git commits
    unless it is genuinely correcting drift, and is fully idempotent.

    The stamp preserves every existing compat key (it does NOT call
    :func:`compat.write_compat_record`, which resets ``required_capabilities`` to
    ``[]``); it appends the token (deduped), writes atomically, and commits the
    COMMITTED record. Keying on the mapping's project count decouples the stamp from
    HOW the store became multi-project — the day a store gains a second project, the
    next sweep stamps it and older binaries then fail closed on it."""
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
