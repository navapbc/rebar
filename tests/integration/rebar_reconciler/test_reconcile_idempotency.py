"""reconcile_once 1-pass idempotency over an unchanged remote (robe-creek-zealot).

Integration-tier counterpart to tests/unit/rebar_reconciler/test_reconcile_once.py.
The unit tier runs against a NON-git tmp repo, so ``rebar list`` exits 1 and
``_read_local_tickets`` returns [] — the outbound differ never sees the
inbound-created tickets and the idempotency assertion is vacuously green
(the "false green" documented on ticket robe-creek-zealot). This tier closes
that gap:

  - the repo root is a REAL git work tree, so ``rebar list`` (the real ticket
    CLI, via subprocess) returns the inbound-created working-tree tickets and
    the outbound differ runs over them;
  - the acli fake is FAITHFUL: label / entity-property write-back and created
    issues are reflected in subsequent searches, exactly like live Jira.

Idempotency contract: pass 1 imports the remote working set (inbound creates,
which now bind_confirm at creation); pass 2 over the unchanged remote computes
ZERO mutations and issues ZERO Jira write calls.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ENGINE_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
RECONCILER_DIR = ENGINE_DIR / "rebar_reconciler"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Faithful stateful Jira fake — every write is reflected on subsequent reads.
# ---------------------------------------------------------------------------


class _FakeJiraState:
    def __init__(self):
        self.issues: dict[str, dict] = {}  # key -> fields dict
        self.props: dict[str, dict] = {}
        self.next_num = 100
        self.write_calls: list[str] = []  # every non-read client call

    def seed(self, key: str, **fields) -> None:
        fields.setdefault("labels", [])
        self.issues[key] = fields


class _FakeClient:
    def __init__(self, state: _FakeJiraState):
        self._s = state

    # reads -----------------------------------------------------------------
    def search_issues(self, jql: str, **kwargs) -> list[dict]:
        s = self._s
        if jql.strip().startswith('labels = "rebar-id:'):
            want = jql.split('"')[1]
            return [
                {"key": k, "fields": json.loads(json.dumps(f))}
                for k, f in s.issues.items()
                if want in (f.get("labels") or [])
            ]
        done_q = '= "Done"' in jql and '!= "Done"' not in jql
        out = []
        for k, f in s.issues.items():
            name = (f.get("status") or {}).get("name", "")
            if done_q == (name == "Done"):
                out.append({"key": k, "fields": json.loads(json.dumps(f))})
        return out

    def get_comments(self, key: str) -> list:
        return []

    def get_issue_by_rest(self, key: str) -> dict:
        return {"key": key, "fields": json.loads(json.dumps(self._s.issues[key]))}

    # writes (all recorded) ---------------------------------------------------
    def create_issue(self, fields: dict) -> dict:
        s = self._s
        key = f"DIG-{s.next_num}"
        s.next_num += 1
        s.write_calls.append(f"create_issue->{key}")
        s.issues[key] = {
            "summary": fields.get("title") or fields.get("summary", ""),
            "status": {"name": "To Do"},
            "issuetype": {"name": (fields.get("ticket_type") or "Task").capitalize()},
            "priority": {"name": "Medium"},
            "labels": [],
        }
        return {"key": key}

    def update_issue(self, key: str, **fields) -> dict:
        self._s.write_calls.append(f"update_issue({key})")
        self._s.issues.setdefault(key, {"labels": []}).update(fields)
        return {"key": key}

    def add_label(self, key: str, label: str) -> None:
        self._s.write_calls.append(f"add_label({key},{label})")
        labels = self._s.issues.setdefault(key, {}).setdefault("labels", [])
        if label not in labels:
            labels.append(label)

    def remove_label(self, key: str, label: str) -> None:
        self._s.write_calls.append(f"remove_label({key},{label})")
        labels = self._s.issues.get(key, {}).get("labels", [])
        if label in labels:
            labels.remove(label)

    def add_comment(self, key: str, body: str) -> dict:
        self._s.write_calls.append(f"add_comment({key})")
        return {"id": "fake-comment"}

    def set_entity_property(self, key: str, prop: str, value) -> None:
        # Property write-back is identity bookkeeping, not sync churn — still
        # recorded so the pass-2 zero-write assertion catches regressions.
        self._s.write_calls.append(f"set_entity_property({key},{prop})")
        self._s.props.setdefault(key, {})[prop] = value

    def transition_issue(self, key: str, status: str) -> None:
        self._s.write_calls.append(f"transition_issue({key},{status})")
        self._s.issues[key]["status"] = {"name": status}

    def transition_issue_by_name(self, key: str, target: str) -> None:
        self.transition_issue(key, target)

    def delete_issue(self, key: str) -> None:
        self._s.write_calls.append(f"delete_issue({key})")
        self._s.issues.pop(key, None)

    def unassign_issue(self, key: str) -> None:
        self._s.write_calls.append(f"unassign_issue({key})")


def _make_fake_acli_module(state: _FakeJiraState) -> types.ModuleType:
    mod = types.ModuleType("acli_integration")
    mod.AcliClient = lambda *a, **k: _FakeClient(state)  # type: ignore[attr-defined]
    return mod


def _make_ok_concurrency() -> types.ModuleType:
    """Pass-through concurrency stub (no git commits; deterministic)."""

    class _Result:
        ok = True
        event = None
        value = None

    mod = types.ModuleType("_concurrency")
    mod.snapshot_head = lambda repo_root: "aabbccdd" * 5  # type: ignore[attr-defined]

    def _rebase_retry(repo_root, write_fn, *, max_attempts=3):
        write_fn()
        return _Result()

    mod.rebase_retry = _rebase_retry  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """A real git work tree with an initialised .tickets-tracker, wired so
    the REAL ticket CLI serves ``rebar list`` for _read_local_tickets."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    (tracker / ".env-id").write_text("integration-env-id", encoding="utf-8")

    monkeypatch.delenv("TICKETS_TRACKER_DIR", raising=False)
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    monkeypatch.delenv("REBAR_ENV_ID", raising=False)
    monkeypatch.delenv("REBAR_AUTHOR", raising=False)
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    # Reconciler reads local tickets via the in-process rebar CLI.
    from rebar._engine import in_process_cli

    monkeypatch.setenv("REBAR_TICKET_CLI", in_process_cli())
    return tmp_path


@pytest.fixture
def reconciler_modules(monkeypatch):
    """Fresh fetcher/applier/reconcile modules registered under the canonical
    sys.modules keys reconcile._load() reuses."""
    # Drop any stale registrations from other test files in this session so
    # reconcile_once loads OUR patched copies.
    for key in (
        "reconcile",
        "reconcile_fetcher",
        "reconcile_applier",
        "acli_integration",
    ):
        monkeypatch.delitem(sys.modules, key, raising=False)
    fetcher = _load_module("reconcile_fetcher", RECONCILER_DIR / "fetcher.py")
    applier = _load_module("reconcile_applier", RECONCILER_DIR / "applier.py")
    reconcile = _load_module("reconcile", RECONCILER_DIR / "reconcile.py")
    return fetcher, applier, reconcile


def _run_pass(reconciler_modules, state, repo_root, monkeypatch, pass_id):
    fetcher, applier, reconcile = reconciler_modules
    fake_mod = _make_fake_acli_module(state)
    # reconcile_once's own outbound-comment client load reuses this key.
    monkeypatch.setitem(sys.modules, "acli_integration", fake_mod)
    monkeypatch.setattr(fetcher, "_load_acli", lambda: fake_mod)
    monkeypatch.setattr(applier, "_load_acli", lambda: fake_mod)
    ok_concurrency = _make_ok_concurrency()
    monkeypatch.setattr(applier, "_load_concurrency", lambda: ok_concurrency)
    return reconcile.reconcile_once(pass_id, repo_root=repo_root)


def _seed_working_set(state: _FakeJiraState) -> None:
    state.seed(
        "DIG-1",
        summary="Implement login",
        status={"name": "In Progress"},
        issuetype={"name": "Story"},
        priority={"name": "High"},
        # Live snapshots carry description as raw ADF.
        description={
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Login flow."}],
                }
            ],
        },
    )
    state.seed(
        "DIG-2",
        summary="Write unit tests",
        status={"name": "To Do"},
        issuetype={"name": "Task"},
        priority={"name": "Medium"},
    )
    state.seed(
        "DIG-3",
        summary="Old finished work",
        status={"name": "Done"},
        issuetype={"name": "Task"},
        priority={"name": "Low"},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pass_2_over_unchanged_remote_is_idempotent(git_repo, reconciler_modules, monkeypatch):
    """Pass 1 imports the remote working set; pass 2 over the UNCHANGED remote
    computes zero mutations and issues zero Jira writes (1-pass idempotency,
    ticket robe-creek-zealot AC)."""
    state = _FakeJiraState()
    _seed_working_set(state)

    result1 = _run_pass(reconciler_modules, state, git_repo, monkeypatch, "ip-pass")
    # Pass 1 imports the three remote issues (inbound creates).
    assert result1["mutation_count"] == 3

    state.write_calls.clear()
    result2 = _run_pass(reconciler_modules, state, git_repo, monkeypatch, "ip-pass")
    assert result2["mutation_count"] == 0, (
        f"pass 2 over an unchanged remote must compute ZERO mutations, got "
        f"{result2['mutation_count']} — the import did not net out "
        f"(write calls: {state.write_calls})"
    )
    assert state.write_calls == [], (
        f"pass 2 must issue ZERO Jira write calls, got: {state.write_calls}"
    )


def test_import_materialises_faithfully_and_binds(git_repo, reconciler_modules, monkeypatch):
    """The pass-1 import lands in the exact state the binding-aware differs
    would compute: canonical status reverse-map, normalised ADF description,
    confirmed binding recorded at creation (not via next-pass dedup-skip)."""
    state = _FakeJiraState()
    _seed_working_set(state)
    _run_pass(reconciler_modules, state, git_repo, monkeypatch, "mat-pass")

    tracker = git_repo / ".tickets-tracker"

    def _events(local_id: str, kind: str) -> list[dict]:
        return [
            json.loads(p.read_text())
            for p in sorted((tracker / local_id).glob("*.json"))
            if f"-{kind}." in p.name
        ]

    # Canonical status reverse-map: In Progress -> in_progress (NOT blocked),
    # Done -> closed (NOT cancelled), To Do -> open (no STATUS event).
    assert [e["data"]["status"] for e in _events("jira-dig-1", "STATUS")] == ["in_progress"]
    assert _events("jira-dig-2", "STATUS") == []
    assert [e["data"]["status"] for e in _events("jira-dig-3", "STATUS")] == ["closed"]

    # ADF description normalised to plain text in the CREATE event.
    (create_1,) = _events("jira-dig-1", "CREATE")
    assert create_1["data"]["description"].strip() == "Login flow."

    # Binding recorded as confirmed at creation time.
    bindings = json.loads((tracker / ".bridge_state" / "bindings.json").read_text())["bindings"]
    for local_id, jira_key in (
        ("jira-dig-1", "DIG-1"),
        ("jira-dig-2", "DIG-2"),
        ("jira-dig-3", "DIG-3"),
    ):
        assert bindings[local_id]["jira_key"] == jira_key
        assert bindings[local_id]["state"] == "confirmed"

    # Label / property write-back reached the (faithful) remote.
    assert "rebar-id:jira-dig-1" in state.issues["DIG-1"]["labels"]
    assert state.props["DIG-1"]["local_id"] == "jira-dig-1"
