"""Parity gate for #2: in-process native reads vs. the bash dispatcher.

The library's public reads run in-process (via ticket_reducer / ticket_graph).
This pins them against the independent **bash engine** (the `rebar` dispatcher,
invoked through ``rebar._engine.run``) across the edge cases the per-read shims
handle differently: archived on/off, alias / short-id / canonical input, missing
& error tickets, blocker permutations, and each filter dimension.

(Originally this compared an in-process vs. a REBAR_NATIVE_READS=0 subprocess
path; that kill-switch was removed in the 0.2.0 cycle, so the oracle is now the
bash dispatcher directly — a stronger, independent implementation.)
"""

from __future__ import annotations

import json

import pytest

import rebar
from rebar._engine import run as _engine_run


def _bash(args, repo):
    """Oracle: run the bash dispatcher and return parsed JSON. Raises RebarError
    on a nonzero exit (mirrors the contract the in-process reads must match)."""
    cp = _engine_run(args, repo_root=repo, check=False, capture=True)
    if cp.returncode != 0:
        raise rebar.RebarError(
            f"bash {args[0]} exit {cp.returncode}: {cp.stderr.strip()}",
            returncode=cp.returncode,
            stderr=cp.stderr,
        )
    return json.loads(cp.stdout)


def _list_args(**kw):
    args = ["list"]
    if kw.get("status"):
        args.append(f"--status={kw['status']}")
    if kw.get("ticket_type"):
        args.append(f"--type={kw['ticket_type']}")
    if kw.get("priority") is not None:
        args.append(f"--priority={kw['priority']}")
    if kw.get("parent"):
        args.append(f"--parent={kw['parent']}")
    if kw.get("has_tag"):
        args.append(f"--has-tag={kw['has_tag']}")
    if kw.get("without_tag"):
        args.append(f"--without-tag={kw['without_tag']}")
    if kw.get("include_archived"):
        args.append("--include-archived")
    return args


def _search_args(query, **kw):
    args = ["search", query]
    if kw.get("status") is not None:
        args.append(f"--status={kw['status']}")
    if kw.get("ticket_type") is not None:
        args.append(f"--type={kw['ticket_type']}")
    if kw.get("has_tag") is not None:
        args.append(f"--has-tag={kw['has_tag']}")
    if kw.get("include_archived"):
        args.append("--include-archived")
    return args


@pytest.fixture
def seeded(rebar_repo):
    """A store covering the read edge cases. Returns (repo, ids, alias)."""
    r = str(rebar_repo)
    ids = {}
    ids["epic"] = rebar.create_ticket("epic", "Epic alpha", repo_root=r)
    ids["story"] = rebar.create_ticket(
        "story", "Story beta login", parent=ids["epic"], repo_root=r
    )
    open_t = rebar.create_ticket(
        "task", "Task gamma login page", tags=["frontend"], return_alias=True, repo_root=r
    )
    ids["task_open"] = open_t["id"]
    alias = open_t["alias"]
    ids["task_blocker"] = rebar.create_ticket("task", "Task delta blocker", repo_root=r)
    ids["bug"] = rebar.create_ticket(
        "bug", "Bug epsilon search term", tags=["backend"], repo_root=r
    )
    ids["archived"] = rebar.create_ticket("task", "Task zeta archived", repo_root=r)

    rebar.link(ids["task_open"], ids["task_blocker"], "depends_on", repo_root=r)
    rebar.archive(ids["archived"], repo_root=r)
    return rebar_repo, ids, alias


# ── list ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"status": "open"},
        {"ticket_type": "task"},
        {"ticket_type": "epic"},
        {"has_tag": "frontend"},
        {"without_tag": "frontend"},
        {"include_archived": True},
        {"priority": 2},
    ],
)
def test_list_parity(seeded, kwargs):
    repo, _ids, _alias = seeded
    lib = rebar.list_tickets(repo_root=str(repo), **kwargs)
    assert lib == _bash(_list_args(**kwargs), str(repo))
    assert isinstance(lib, list)


def test_list_parent_filter_parity(seeded):
    repo, ids, _alias = seeded
    lib = rebar.list_tickets(parent=ids["epic"], repo_root=str(repo))
    assert lib == _bash(_list_args(parent=ids["epic"]), str(repo))
    assert any(t["ticket_id"] == ids["story"] for t in lib)


# ── show ─────────────────────────────────────────────────────────────────────
def test_show_parity_canonical(seeded):
    repo, ids, _alias = seeded
    lib = rebar.show_ticket(ids["task_open"], repo_root=str(repo))
    assert lib == _bash(["show", ids["task_open"]], str(repo))
    assert lib["ticket_id"] == ids["task_open"]


def test_show_parity_alias(seeded):
    repo, ids, alias = seeded
    lib = rebar.show_ticket(alias, repo_root=str(repo))
    assert lib == _bash(["show", alias], str(repo))
    assert lib["ticket_id"] == ids["task_open"]


def test_show_parity_short_id(seeded):
    repo, ids, _alias = seeded
    short = ids["task_open"].split("-")[0]
    lib = rebar.show_ticket(short, repo_root=str(repo))
    assert lib == _bash(["show", short], str(repo))
    assert lib["ticket_id"] == ids["task_open"]


def test_show_missing_raises_both(seeded):
    repo, _ids, _alias = seeded
    with pytest.raises(rebar.RebarError):
        rebar.show_ticket("nope-nope-nope-nope", repo_root=str(repo))
    with pytest.raises(rebar.RebarError):
        _bash(["show", "nope-nope-nope-nope"], str(repo))


# ── search ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "args,kwargs",
    [
        (("login",), {}),
        (("search term",), {}),
        (("task",), {"status": "open"}),
        (("task",), {"ticket_type": "task"}),
        (("zeta",), {}),
        (("zeta",), {"include_archived": True}),
        (("frontend",), {"has_tag": "frontend"}),
        (("definitely-no-match-xyz",), {}),
    ],
)
def test_search_parity(seeded, args, kwargs):
    repo, _ids, _alias = seeded
    lib = rebar.search(*args, repo_root=str(repo), **kwargs)
    assert lib == _bash(_search_args(*args, **kwargs), str(repo))


# ── ready ────────────────────────────────────────────────────────────────────
def test_ready_parity(seeded):
    repo, ids, _alias = seeded
    lib = rebar.ready(repo_root=str(repo))
    assert lib == _bash(["ready", "--output", "json"], str(repo))
    ready_ids = {t["ticket_id"] for t in lib}
    assert ids["task_open"] not in ready_ids
    assert ids["task_blocker"] in ready_ids


def test_ready_parity_after_unblock(seeded):
    repo, ids, _alias = seeded
    rebar.claim(ids["task_blocker"], assignee="t", repo_root=str(repo))
    rebar.transition(ids["task_blocker"], "in_progress", "closed", repo_root=str(repo))
    lib = rebar.ready(repo_root=str(repo))
    assert lib == _bash(["ready", "--output", "json"], str(repo))
    assert ids["task_open"] in {t["ticket_id"] for t in lib}


# ── deps ─────────────────────────────────────────────────────────────────────
def test_deps_parity(seeded):
    repo, ids, _alias = seeded
    lib = rebar.deps(ids["task_open"], repo_root=str(repo))
    assert lib == _bash(["deps", ids["task_open"]], str(repo))
    assert lib["ticket_id"] == ids["task_open"]


def test_deps_archived_target_raises_both(seeded):
    repo, ids, _alias = seeded
    with pytest.raises(rebar.RebarError):
        rebar.deps(ids["archived"], repo_root=str(repo))
    with pytest.raises(rebar.RebarError):
        _bash(["deps", ids["archived"]], str(repo))


# ── empty store ──────────────────────────────────────────────────────────────
def test_empty_store_parity(rebar_repo):
    repo = str(rebar_repo)
    assert rebar.list_tickets(repo_root=repo) == _bash(["list"], repo)
    assert rebar.ready(repo_root=repo) == _bash(["ready", "--output", "json"], repo)
    assert rebar.search("anything", repo_root=repo) == _bash(["search", "anything"], repo)
