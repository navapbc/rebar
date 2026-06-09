"""Parity gate for #2: in-process native reads vs. the subprocess dispatcher.

Every public read must return the SAME object whether it runs in-process
(``REBAR_NATIVE_READS`` default) or via the bash dispatcher
(``REBAR_NATIVE_READS=0``), across the edge cases the per-read shims handle
differently: archived on/off, alias / short-id / canonical input, missing &
error tickets, blocker permutations, and each filter dimension.

Writes always go through the subprocess path, so the fixture seeds identically
for both read modes.
"""

from __future__ import annotations

import pytest

import rebar


def _native(monkeypatch, fn):
    monkeypatch.delenv("REBAR_NATIVE_READS", raising=False)
    return fn()


def _subprocess(monkeypatch, fn):
    monkeypatch.setenv("REBAR_NATIVE_READS", "0")
    try:
        return fn()
    finally:
        monkeypatch.delenv("REBAR_NATIVE_READS", raising=False)


def _assert_parity(monkeypatch, fn):
    """fn() under both modes must be equal; returns the (shared) result."""
    native = _native(monkeypatch, fn)
    sub = _subprocess(monkeypatch, fn)
    assert native == sub, f"native != subprocess\n native={native!r}\n sub={sub!r}"
    return native


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

    # Blocker edge: task_open depends_on task_blocker (still open → blocks).
    rebar.link(ids["task_open"], ids["task_blocker"], "depends_on", repo_root=r)
    # Archived edge.
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
def test_list_parity(monkeypatch, seeded, kwargs):
    repo, ids, _alias = seeded
    res = _assert_parity(
        monkeypatch, lambda: rebar.list_tickets(repo_root=str(repo), **kwargs)
    )
    assert isinstance(res, list)


def test_list_parent_filter_parity(monkeypatch, seeded):
    repo, ids, _alias = seeded
    res = _assert_parity(
        monkeypatch, lambda: rebar.list_tickets(parent=ids["epic"], repo_root=str(repo))
    )
    assert any(t["ticket_id"] == ids["story"] for t in res)


# ── show ─────────────────────────────────────────────────────────────────────
def test_show_parity_canonical(monkeypatch, seeded):
    repo, ids, _alias = seeded
    res = _assert_parity(
        monkeypatch, lambda: rebar.show_ticket(ids["task_open"], repo_root=str(repo))
    )
    assert res["ticket_id"] == ids["task_open"]


def test_show_parity_alias(monkeypatch, seeded):
    repo, ids, alias = seeded
    res = _assert_parity(monkeypatch, lambda: rebar.show_ticket(alias, repo_root=str(repo)))
    assert res["ticket_id"] == ids["task_open"]


def test_show_parity_short_id(monkeypatch, seeded):
    repo, ids, _alias = seeded
    short = ids["task_open"].split("-")[0]  # first hex group
    res = _assert_parity(monkeypatch, lambda: rebar.show_ticket(short, repo_root=str(repo)))
    assert res["ticket_id"] == ids["task_open"]


def test_show_missing_raises_both_modes(monkeypatch, seeded):
    repo, _ids, _alias = seeded

    def call():
        return rebar.show_ticket("nope-nope-nope-nope", repo_root=str(repo))

    monkeypatch.delenv("REBAR_NATIVE_READS", raising=False)
    with pytest.raises(rebar.RebarError):
        call()
    monkeypatch.setenv("REBAR_NATIVE_READS", "0")
    with pytest.raises(rebar.RebarError):
        call()
    monkeypatch.delenv("REBAR_NATIVE_READS", raising=False)


# ── search ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "args,kwargs",
    [
        (("login",), {}),
        (("search term",), {}),
        (("task",), {"status": "open"}),
        (("task",), {"ticket_type": "task"}),
        (("zeta",), {}),  # archived ticket — excluded by default
        (("zeta",), {"include_archived": True}),
        (("frontend",), {"has_tag": "frontend"}),
        (("definitely-no-match-xyz",), {}),
    ],
)
def test_search_parity(monkeypatch, seeded, args, kwargs):
    repo, _ids, _alias = seeded
    _assert_parity(
        monkeypatch, lambda: rebar.search(*args, repo_root=str(repo), **kwargs)
    )


# ── ready ────────────────────────────────────────────────────────────────────
def test_ready_parity(monkeypatch, seeded):
    repo, ids, _alias = seeded
    res = _assert_parity(monkeypatch, lambda: rebar.ready(repo_root=str(repo)))
    ready_ids = {t["ticket_id"] for t in res}
    # task_open is blocked by an open task_blocker → not ready.
    assert ids["task_open"] not in ready_ids
    assert ids["task_blocker"] in ready_ids


def test_ready_parity_after_unblock(monkeypatch, seeded):
    repo, ids, _alias = seeded
    rebar.claim(ids["task_blocker"], assignee="t", repo_root=str(repo))
    rebar.transition(ids["task_blocker"], "in_progress", "closed", repo_root=str(repo))
    res = _assert_parity(monkeypatch, lambda: rebar.ready(repo_root=str(repo)))
    assert ids["task_open"] in {t["ticket_id"] for t in res}


# ── deps ─────────────────────────────────────────────────────────────────────
def test_deps_parity(monkeypatch, seeded):
    repo, ids, _alias = seeded
    res = _assert_parity(monkeypatch, lambda: rebar.deps(ids["task_open"], repo_root=str(repo)))
    assert res["ticket_id"] == ids["task_open"]


def test_deps_archived_target_raises_both_modes(monkeypatch, seeded):
    repo, ids, _alias = seeded

    def call():
        return rebar.deps(ids["archived"], repo_root=str(repo))

    monkeypatch.delenv("REBAR_NATIVE_READS", raising=False)
    with pytest.raises(rebar.RebarError):
        call()
    monkeypatch.setenv("REBAR_NATIVE_READS", "0")
    with pytest.raises(rebar.RebarError):
        call()
    monkeypatch.delenv("REBAR_NATIVE_READS", raising=False)


# ── empty store ──────────────────────────────────────────────────────────────
def test_empty_store_parity(monkeypatch, rebar_repo):
    repo = str(rebar_repo)
    _assert_parity(monkeypatch, lambda: rebar.list_tickets(repo_root=repo))
    _assert_parity(monkeypatch, lambda: rebar.ready(repo_root=repo))
    _assert_parity(monkeypatch, lambda: rebar.search("anything", repo_root=repo))
