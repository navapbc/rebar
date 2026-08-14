"""Held-out oracle for the legacy-store projects-mapping migration (ticket 462d).

Two ensure units converge an existing store onto the many-to-many projects model
WITHOUT writing per-ticket events:

- **seed** — a store with no ``.bridge_state/projects.json`` gains one whose
  ``legacy_default`` is the backend's effective project, so every absent-field
  ticket resolves to it (one small write migrates the whole store).
- **stamp** — a *convergence backstop* that adds the ``multi-project-bridge``
  compat capability ONLY when the mapping already holds more than one project (the
  authoritative write-time stamp is the set/remove write path's job; this unit just
  repairs residual divergence). A single-project store is never stamped.

Everything here asserts OBSERVABLE state — committed tickets-branch blobs, the
event-file set, the tickets tip SHA, and the compat gate's raise/no-raise — never
private names, so a behaviour-preserving refactor of the units leaves it green.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import config as _config
from rebar._lib_ops import _engine_module
from rebar._store import compat, ensures
from rebar._store.compat import StoreIncompatibleError


def _projects_store():
    """The embedded ``rebar_reconciler.projects_store`` module, loaded through the
    supported engine loader (it is not importable as ``rebar._engine.*``)."""
    return _engine_module("rebar_reconciler.projects_store")


_ORIGINAL_UNIT_IDS = {
    "env-id",
    "gc-config",
    "merge-ours",
    "gitattributes",
    "gitignore",
    "store-compat",
}

# The effective backend project this suite pins via rebar.toml; distinct from the
# ACLI create-time default ("DIG") so a seeded legacy_default proves the unit read
# the store's configured project rather than the fallback.
_PROJECT = "REB"


def _git(tracker: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(tracker), *args], capture_output=True, text=True)


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _tickets_head(tracker: Path) -> str:
    return _git(tracker, "rev-parse", "tickets").stdout.strip()


def _show(tracker: Path, path: str) -> subprocess.CompletedProcess[str]:
    return _git(tracker, "show", f"tickets:{path}")


def _committed_json(tracker: Path, path: str) -> dict:
    got = _show(tracker, path)
    assert got.returncode == 0, f"tickets:{path} is not committed"
    return json.loads(got.stdout)


def _event_files(tracker: Path) -> set[str]:
    """The set of committed ticket-event files (``*.json`` that parse to a dict
    carrying ``event_type``), relative to the tracker. Excludes sidecars like
    ``.bridge_state/projects.json`` and ``.store-compat.json`` which are not events."""
    out: set[str] = set()
    for p in tracker.rglob("*.json"):
        try:
            obj = json.loads(p.read_bytes())
        except (ValueError, OSError):
            continue
        if isinstance(obj, dict) and "event_type" in obj:
            out.add(str(p.relative_to(tracker)))
    return out


def _commit_mapping(tracker: Path, record: dict) -> None:
    """Write + commit a ``.bridge_state/projects.json`` record onto the tickets
    branch (mirrors how a real mapping mutation persists), so a sweep observes it."""
    dest = tracker / ".bridge_state" / "projects.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert _git(tracker, "add", ".bridge_state/projects.json").returncode == 0
    assert _git(tracker, "commit", "-q", "--no-verify", "-m", "test: set mapping").returncode == 0


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A freshly initialized rebar store whose configured backend project is
    ``REB``. ``init_repo`` runs the ensure sweep, so the migration units (once
    implemented) have already run on the returned store."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "base"], cwd=repo, check=True)
    (repo / "rebar.toml").write_text(f"[jira]\nproject = '{_PROJECT}'\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.delenv("JIRA_PROJECT", raising=False)
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    _config.reset_config_cache()
    rebar.init_repo(repo_root=str(repo))
    ensures._reset_pending_cache()
    return repo


# ── happy path: seed a no-mapping store from the backend project ─────────────
def test_seed_creates_mapping_from_backend_project(store: Path) -> None:
    """A legacy store (existing tickets, no mapping) gains a projects.json whose
    legacy_default is the configured backend project, with that project as its single
    entry, and every absent-``bridge_project`` ticket resolves to it."""
    tracker = _tracker(store)
    rebar.create_ticket("task", "legacy ticket", repo_root=str(store))
    ensures.run_ensures(tracker)
    record = _committed_json(tracker, ".bridge_state/projects.json")

    assert record["legacy_default"] == _PROJECT
    assert _PROJECT in record["projects"]

    mapping = _projects_store().load_mapping(store)
    assert mapping.legacy_default == _PROJECT
    assert _projects_store().resolve_project({}, mapping) == _PROJECT
    ps = _projects_store()
    assert ps.resolve_project({"title": "no bridge_project field"}, mapping) == _PROJECT


# ── the migration writes ZERO ticket events ──────────────────────────────────
def test_seed_writes_no_ticket_events(store: Path) -> None:
    """The seed migrates the whole store with ONE sidecar write — not an
    EDIT-per-ticket sweep. After de-seeding, a fresh sweep re-creates the mapping
    while the committed ticket-event file set is byte-for-byte identical."""
    tracker = _tracker(store)
    rebar.create_ticket("task", "one", repo_root=str(store))
    rebar.create_ticket("task", "two", repo_root=str(store))

    # Force the seed to run again by removing the committed mapping (best-effort:
    # before the seed exists there is nothing to remove, and the real assertion
    # below — that a sweep re-creates it — is what must fail then).
    if _show(tracker, ".bridge_state/projects.json").returncode == 0:
        _git(tracker, "rm", "-q", ".bridge_state/projects.json")
        _git(tracker, "commit", "-q", "--no-verify", "-m", "de-seed")
    events_before = _event_files(tracker)
    assert events_before, "expected committed ticket-event files to compare against"

    ensures.run_ensures(tracker)

    assert _show(tracker, ".bridge_state/projects.json").returncode == 0, "seed did not re-create"
    assert _event_files(tracker) == events_before, "the seed must not write ticket events"


# ── idempotence: a converged sweep makes no commits ──────────────────────────
def test_second_sweep_is_idempotent(store: Path) -> None:
    """A second sweep on the converged store returns ``ok`` for the new units and
    creates zero commits (the tickets tip SHA is unchanged)."""
    tracker = _tracker(store)
    before = _tickets_head(tracker)

    outcomes = ensures.run_ensures(tracker)

    assert _tickets_head(tracker) == before, "converged sweep must not create commits"
    by_id = {o.id: o for o in outcomes}
    new_ids = set(ensures.REGISTRY_IDS) - _ORIGINAL_UNIT_IDS
    assert len(new_ids) == 2, "the migration must register exactly two new ensure units"
    for uid in new_ids:
        assert by_id[uid].status == "ok", (uid, outcomes)


# ── conditional stamp (contrast pair) ────────────────────────────────────────
def test_single_project_store_is_not_stamped(store: Path) -> None:
    """A one-project mapping (the seed only ever makes one) is never stamped — an
    older binary can still read the store."""
    tracker = _tracker(store)
    rebar.create_ticket("task", "legacy ticket", repo_root=str(store))
    ensures.run_ensures(tracker)
    caps = _committed_json(tracker, compat.COMPAT_FILENAME)["required_capabilities"]
    assert "multi-project-bridge" not in caps


def test_multi_project_store_is_stamped(store: Path) -> None:
    """When the mapping holds more than one project, the backstop stamps exactly
    ``multi-project-bridge`` into the committed compat record."""
    tracker = _tracker(store)
    _commit_mapping(
        tracker,
        {
            "version": 1,
            "legacy_default": _PROJECT,
            "projects": {_PROJECT: {"repos": ["rebar"]}, "DIG": {"repos": ["dig"]}},
        },
    )

    ensures.run_ensures(tracker)

    caps = _committed_json(tracker, compat.COMPAT_FILENAME)["required_capabilities"]
    assert "multi-project-bridge" in caps


def test_multi_project_stamped_store_still_passes_compat_gate(store: Path) -> None:
    """A two-project stamped store still passes ``check_store_compat`` on this
    binary — the token is a KNOWN capability, so stamping never self-locks-out."""
    tracker = _tracker(store)
    _commit_mapping(
        tracker,
        {
            "version": 1,
            "legacy_default": _PROJECT,
            "projects": {_PROJECT: {"repos": ["rebar"]}, "DIG": {"repos": ["dig"]}},
        },
    )
    ensures.run_ensures(tracker)

    assert (
        "multi-project-bridge"
        in _committed_json(tracker, compat.COMPAT_FILENAME)["required_capabilities"]
    )
    compat.check_store_compat(tracker)  # must NOT raise
    try:
        compat.check_store_compat(tracker)
    except StoreIncompatibleError as exc:  # pragma: no cover - defensive
        pytest.fail(f"stamped store rejected by its own compat gate: {exc}")


# ── registry wiring + failure isolation ──────────────────────────────────────
def test_new_units_are_registered(store: Path) -> None:
    """The two new units are registered on the frozen id set AND the callable map,
    so the sweep actually runs them (a rename/typo would strand one forever-pending)."""
    new_ids = set(ensures.REGISTRY_IDS) - _ORIGINAL_UNIT_IDS
    assert len(new_ids) == 2
    assert new_ids <= set(ensures._registry().keys())
    assert ensures.registry_ids() == frozenset(ensures.REGISTRY_IDS)


def test_raising_migration_unit_is_recorded_failed(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration unit that raises is caught (recorded ``failed``) and does not
    abort the sweep or its caller — the other units still run."""
    tracker = _tracker(store)
    new_ids = sorted(set(ensures.REGISTRY_IDS) - _ORIGINAL_UNIT_IDS)
    assert len(new_ids) == 2
    target = new_ids[0]

    real = ensures._registry()

    def _explode(_tracker: str) -> ensures.EnsureOutcome:
        raise RuntimeError("boom")

    monkeypatch.setattr(ensures, "_registry", lambda: {**real, target: _explode})
    outcomes = ensures.run_ensures(tracker)  # must NOT raise

    by_id = {o.id: o for o in outcomes}
    assert by_id[target].status == "failed"
    assert all(by_id[u].status in ("ok", "changed") for u in ensures.REGISTRY_IDS if u != target)
    assert target not in ensures.applied_ids(tracker)
