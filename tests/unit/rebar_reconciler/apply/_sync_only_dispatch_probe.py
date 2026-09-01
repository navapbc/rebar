"""HELD-OUT oracle (rebar-debug, bug af1b) — a scoped `sync --only` LIVE pass MUST
dispatch a bound issue's outbound scalar UPDATE to the transport write, exactly as the
legacy `--filter-local-ids` route does.

Confirmed root cause: run_differs builds the outbound update with ``target = jira_key``
and hands ``ticket_planner.plan_pass`` a selection whose ``ids`` are the SELECTED LOCAL
IDS only. ``_scope_excluded`` then compares the jira-key target against those local ids
(``target not in ids``), classifies the in-scope bound-issue update as ``scope_deferred``,
and the live coordinator+fuse reroute (batch_dispatch) skips the deferred plan — the write
is dropped. The legacy route works because it scopes via ``_build_filter_target_set``
(LOCAL IDS ∪ their bound JIRA KEYS). This oracle pins the write actually landing on the
primary route (the teeth: it is RED before the fix for ``--only`` and GREEN for
``--filter-local-ids``).

No live Jira: a faithful in-memory transport records ``update_issue`` calls. The bug is not
codec/DC-specific, so an offline transport reproduces it (matching the live in-CI probe for
bug af1b; context: external, GH Actions run 33129851229).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
import urllib.error
import uuid as _uuid
from pathlib import Path
from typing import Any

from rebar._store.ticket_layout import ticket_dir

_ENGINE_DIR = Path(__file__).resolve().parents[4] / "src" / "rebar" / "_engine"
if str(_ENGINE_DIR) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(_ENGINE_DIR))
_RECONCILER_DIR = _ENGINE_DIR / "rebar_reconciler"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeJiraState:
    def __init__(self) -> None:
        self.issues: dict[str, dict] = {}
        self.props: dict[str, dict] = {}
        self.next_num = 100
        self.write_calls: list[str] = []
        self.deleted: set[str] = set()

    def seed(self, key: str, **fields: Any) -> None:
        fields.setdefault("labels", [])
        self.issues[key] = fields


class _FakeClient:
    """In-memory Jira transport that records every write call."""

    def __init__(self, state: _FakeJiraState) -> None:
        self._s = state

    def search_issues(self, jql: str, **_kw: Any) -> list[dict]:
        s = self._s
        if jql.strip().startswith('labels = "rebar-id:'):
            want = jql.split('"')[1]
            return [
                {"key": k, "fields": json.loads(json.dumps(f))}
                for k, f in s.issues.items()
                if want in (f.get("labels") or []) and k not in s.deleted
            ]
        done_q = '= "Done"' in jql and '!= "Done"' not in jql
        out = []
        for k, f in s.issues.items():
            if k in s.deleted:
                continue
            name = (f.get("status") or {}).get("name", "")
            if done_q == (name == "Done"):
                out.append({"key": k, "fields": json.loads(json.dumps(f))})
        return out

    def get_comments(self, _key: str) -> list:
        return []

    def get_issue_by_rest(self, key: str) -> dict:
        if key in self._s.deleted:
            raise urllib.error.HTTPError(
                f"https://fake.test/rest/api/2/issue/{key}", 404, "Not Found", None, None
            )
        return {"key": key, "fields": json.loads(json.dumps(self._s.issues[key]))}

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

    def update_issue(self, key: str, **fields: Any) -> dict:
        self._s.write_calls.append(f"update_issue({key}) fields={sorted(fields)}")
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

    def add_comment(self, key: str, _body: str) -> dict:
        self._s.write_calls.append(f"add_comment({key})")
        return {"id": "fake-comment"}

    def set_entity_property(self, key: str, prop: str, value: Any) -> None:
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


def _make_ok_concurrency() -> types.ModuleType:
    class _Result:
        ok = True
        event = None
        value = None

    mod = types.ModuleType("_concurrency")
    mod.snapshot_head = lambda _repo_root: "aabbccdd" * 5  # type: ignore[attr-defined]

    def _rebase_retry(_repo_root: Any, write_fn: Any, *, max_attempts: int = 3) -> Any:
        write_fn()
        return _Result()

    mod.rebase_retry = _rebase_retry  # type: ignore[attr-defined]
    return mod


def _fresh_modules() -> tuple[Any, Any, Any]:
    for key in (
        "reconcile",
        "reconcile_fetcher",
        "reconcile_applier",
        "reconcile_run_differs",
        "acli_integration",
    ):
        sys.modules.pop(key, None)
    fetcher = _load_module("reconcile_fetcher", _RECONCILER_DIR / "fetcher.py")
    applier = _load_module("reconcile_applier", _RECONCILER_DIR / "applier.py")
    reconcile = _load_module("reconcile", _RECONCILER_DIR / "reconcile.py")
    _load_module("reconcile_run_differs", _RECONCILER_DIR / "run_differs.py")
    return fetcher, applier, reconcile


def _wire(fetcher: Any, applier: Any, state: _FakeJiraState) -> _FakeClient:
    fake = _FakeClient(state)
    sys.modules["acli_integration"] = fake  # type: ignore[assignment]
    fetcher._load_acli = lambda: fake
    applier._load_acli = lambda: fake
    applier._load_concurrency = lambda: _make_ok_concurrency()
    from rebar_reconciler.adapters.jira.backend import JiraBackend

    rd = sys.modules["reconcile_run_differs"]
    rd._load_reconcile_backend = lambda: JiraBackend(fake)  # type: ignore[attr-defined]
    return fake


def _setup_repo(base: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(base)], check=True)
    subprocess.run(["git", "-C", str(base), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(base), "config", "user.name", "t"], check=True)
    tracker = base / ".tickets-tracker"
    tracker.mkdir()
    (tracker / ".env-id").write_text("repro-env-id", encoding="utf-8")
    return base


def _seed(state: _FakeJiraState) -> None:
    state.seed(
        "DIG-1",
        summary="Implement login",
        status={"name": "To Do"},
        issuetype={"name": "Task"},
        priority={"name": "Medium"},
        description="Original body.",
    )


def _prep_scenario(base: Path) -> tuple[_FakeJiraState, Path, Any]:
    os.environ["REBAR_SYNC_PULL"] = "off"
    os.environ["REBAR_SYNC_PUSH"] = "off"
    os.environ["JIRA_PROJECT"] = "DIG"
    os.environ["JIRA_URL"] = "https://example.atlassian.net"
    os.environ["JIRA_USER"] = "reconciler-tests@example.com"
    os.environ["JIRA_API_TOKEN"] = "test-api-token"
    for stale in ("REBAR_TRACKER_DIR", "REBAR_ENV_ID", "REBAR_AUTHOR"):
        os.environ.pop(stale, None)

    repo = _setup_repo(base)
    state = _FakeJiraState()
    _seed(state)

    fetcher, applier, reconcile = _fresh_modules()
    _wire(fetcher, applier, state)
    reconcile.reconcile_once("import-pass", repo_root=repo)

    # Edit the local ticket's description so the outbound differ computes a scalar UPDATE.
    tdir = Path(ticket_dir(repo / ".tickets-tracker", "jira-dig-1"))
    ts = 1_800_000_000_000_000_000
    u = str(_uuid.uuid4())
    ev = {
        "author": "reconciler",
        "author_email": "t@t",
        "data": {"fields": {"description": "Changed body via local edit."}},
        "env_id": "reconciler",
        "event_type": "EDIT",
        "timestamp": ts,
        "uuid": u,
    }
    (tdir / f"{ts}-{u}-EDIT.json").write_text(json.dumps(ev), encoding="utf-8")
    subprocess.run(["git", "-C", str(repo / ".tickets-tracker"), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo / ".tickets-tracker"), "commit", "-q", "-m", "edit"],
        check=True,
    )
    return state, repo, reconcile


def _run_scoped_pass(base: Path, **pass_kwargs: Any) -> list[str]:
    state, repo, _reconcile = _prep_scenario(base)
    fetcher, applier, rc = _fresh_modules()
    _wire(fetcher, applier, state)
    state.write_calls.clear()
    from rebar_reconciler.mode import Mode

    rc.reconcile_once("scoped-pass", repo_root=repo, target_mode=Mode.LIVE, **pass_kwargs)
    return [c for c in state.write_calls if c.startswith("update_issue")]


def _main(argv: list[str]) -> int:
    """CLI entry: `python _sync_only_dispatch_probe.py <route> <base_dir>` where route is
    `only` or `filter`. Prints a JSON list of the recorded update_issue calls to stdout.

    Run as a clean subprocess so pytest's package/conftest module seeding cannot collide
    with the flat-key module (re)loading this in-memory reconcile pass performs.
    """
    route, base = argv[1], Path(argv[2])
    if route == "only":
        updates = _run_scoped_pass(
            base, selection_kind="only", selection_ids={"jira-dig-1"}, route="sync"
        )
    elif route == "filter":
        updates = _run_scoped_pass(base, filter_local_ids={"jira-dig-1"}, route=None)
    else:  # pragma: no cover - guarded by caller
        raise SystemExit(f"unknown route {route!r}")
    print("PROBE_RESULT " + json.dumps(updates))
    return 0


if __name__ == "__main__":  # pragma: no cover - subprocess entry
    raise SystemExit(_main(sys.argv))
