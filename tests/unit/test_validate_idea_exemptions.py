"""`idea` tickets are exempt from noisy `validate` health checks (story 1982).

`idea` is a parking lot for captured-but-undesigned work — often un-parented, terse,
or an empty epic. The health checks that assume a *designed* ticket would punish that
looseness and drag the store-health score down (the "noisy status" problem migrating
into `validate`). So those checks skip `idea` tickets **per-check**, while the genuinely
structural checks stay active on `idea` tickets too.

Exercised at the check-function level with normalized issue dicts (the shape
``validate.normalize_issues`` produces: ``id``/``type``/``status``/``parent``/
``description``/``dependencies``). Each exempted check is pinned with a control (open
ticket → finding fires) and the idea case (→ no finding); the retained structural
check (duplicate titles) is pinned to STILL fire for idea tickets.
"""

from __future__ import annotations

from rebar._engine_support import validate_checks as vc


def _issue(iid, status, itype="task", parent=None, title=None, desc="a real body", deps=None):
    return {
        "id": iid,
        "status": status,
        "type": itype,
        "parent": parent,
        "title": title if title is not None else f"Ticket {iid}",
        "description": desc,
        "notes": "",
        "dependencies": deps or [],
        "created_at": "2026-01-01T09:00:00",
    }


def _sev(findings):
    return [f.severity for f in findings]


def _msgs(findings):
    return " || ".join(f.message for f in findings)


# ── check_orphaned_tasks ──────────────────────────────────────────────────────
def test_orphaned_exempts_idea_but_not_open():
    open_orphan = _issue("o1", "open", parent=None, title="Open orphan")
    idea_orphan = _issue("o2", "idea", parent=None, title="Idea orphan")

    control = vc.check_orphaned_tasks([open_orphan])
    assert any(s == "warning" for s in _sev(control)), "open orphan should warn"

    exempt = vc.check_orphaned_tasks([idea_orphan])
    assert "warning" not in _sev(exempt), "idea orphan must not warn"


def test_orphaned_creation_hour_cluster_ignores_idea():
    # Three un-parented idea tickets in the same creation hour would be a MAJOR
    # cluster if they were open — as ideas they must produce nothing.
    ideas = [_issue(f"c{i}", "idea", parent=None) for i in range(3)]
    findings = vc.check_orphaned_tasks(ideas)
    assert "major" not in _sev(findings)
    assert "warning" not in _sev(findings)


# ── check_empty_epics ─────────────────────────────────────────────────────────
def test_empty_epic_finding_suppressed_for_idea():
    open_empty = _issue("e1", "open", itype="epic", title="Open empty epic")
    idea_empty = _issue("e2", "idea", itype="epic", title="Idea empty epic")

    control = vc.check_empty_epics([open_empty])
    assert "Epic with 0 children" in _msgs(control), "open empty epic should be flagged"

    exempt = vc.check_empty_epics([idea_empty])
    assert "Epic with 0 children" not in _msgs(exempt), "idea empty epic must not be flagged"


# ── check_ticket_count ────────────────────────────────────────────────────────
def test_ticket_count_excludes_idea_from_scored_band():
    # 300 tickets crosses the WARNING band; as ideas they don't count as load.
    ideas = [_issue(f"i{i}", "idea") for i in range(300)]
    findings = vc.check_ticket_count(ideas)
    assert "warning" not in _sev(findings) and "major" not in _sev(findings)

    opens = [_issue(f"o{i}", "open", parent="e") for i in range(300)]
    control = vc.check_ticket_count(opens)
    assert "warning" in _sev(control) or "major" in _sev(control)


# ── check_missing_descriptions ────────────────────────────────────────────────
def test_missing_description_exempts_idea():
    open_bare = _issue("d1", "open", desc="", title="Open bare task")
    idea_bare = _issue("d2", "idea", desc="", title="Idea bare task")

    control = vc.check_missing_descriptions([open_bare])
    assert "Task missing description" in _msgs(control)

    exempt = vc.check_missing_descriptions([idea_bare])
    assert "Task missing description" not in _msgs(exempt)


# ── check_interface_contracts ─────────────────────────────────────────────────
def test_interface_contract_exempts_idea():
    open_iface = _issue("if1", "open", title="Design the widget interface", desc="")
    idea_iface = _issue("if2", "idea", title="Design the widget interface", desc="")

    control = vc.check_interface_contracts([open_iface], "rebar")
    assert "may need documentation" in _msgs(control)

    exempt = vc.check_interface_contracts([idea_iface], "rebar")
    assert "may need documentation" not in _msgs(exempt)


# ── retained structural check: duplicate titles STAYS active for idea ─────────
def test_duplicate_titles_still_fires_for_idea():
    dupes = [
        _issue("t1", "idea", title="Colliding Title"),
        _issue("t2", "idea", title="Colliding Title"),
    ]
    findings = vc.check_duplicate_titles(dupes)
    assert any(f.severity == "minor" and "Colliding Title" in f.message for f in findings), (
        "duplicate-title structural check must still fire for idea tickets"
    )
