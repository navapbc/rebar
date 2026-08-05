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

import ast
import os
import time
from pathlib import Path

import pytest

from rebar._engine_support import validate_checks as vc

_VALIDATE_CHECKS_SRC = (Path(vc.__file__).resolve().parent / "validate_checks.py").read_text(
    encoding="utf-8"
)


def _issue(
    iid,
    status,
    itype="task",
    parent=None,
    title=None,
    desc="a real body",
    deps=None,
    created_at="2026-01-01T09:00:00",
):
    return {
        "id": iid,
        "status": status,
        "type": itype,
        "parent": parent,
        "title": title if title is not None else f"Ticket {iid}",
        "description": desc,
        "notes": "",
        "dependencies": deps or [],
        "created_at": created_at,
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


# ── retained structural checks: child->parent + cross-epic deps STAY active for idea ──
def test_child_parent_dep_still_fires_for_idea():
    # An idea child that depends on its OWN parent is still the CRITICAL anti-pattern —
    # check_child_parent_deps has no idea exemption (structural integrity, not noise).
    child = _issue(
        "c1",
        "idea",
        parent="epicA",
        deps=[
            {"type": "parent-child", "depends_on_id": "epicA"},
            {"type": "blocks", "depends_on_id": "epicA"},
        ],
    )
    findings = vc.check_child_parent_deps([child])
    assert any(f.severity == "critical" and "Child->parent" in f.message for f in findings), (
        "child->parent dependency check must still fire for idea tickets"
    )


def test_cross_epic_child_dep_still_fires_for_idea():
    # An idea child of epicA depending on a child of a DIFFERENT epic is still CRITICAL —
    # check_cross_epic_child_deps has no idea exemption.
    c1 = _issue("c1", "idea", parent="epicA", deps=[{"type": "blocks", "depends_on_id": "c2"}])
    c2 = _issue("c2", "open", parent="epicB")
    findings = vc.check_cross_epic_child_deps([c1, c2])
    assert any(f.severity == "critical" and "Cross-epic" in f.message for f in findings), (
        "cross-epic child dependency check must still fire for idea tickets"
    )


# ── DTZ ratchet: validate_checks orphan-cluster uses AWARE UTC datetimes (story 28cd) ──
def _is_timezone_utc(node) -> bool:
    """True iff ``node`` is the AST for ``timezone.utc``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "utc"
        and isinstance(node.value, ast.Name)
        and node.value.id == "timezone"
    )


def _calls_named(tree, attr):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == attr
        ):
            yield node


def test_source_fromtimestamp_is_timezone_aware():
    tree = ast.parse(_VALIDATE_CHECKS_SRC)
    calls = list(_calls_named(tree, "fromtimestamp"))
    assert calls, "expected a datetime.fromtimestamp call in validate_checks.py"
    for call in calls:
        tz_kw = next((kw for kw in call.keywords if kw.arg == "tz"), None)
        assert tz_kw is not None, "every fromtimestamp call must pass tz="
        assert _is_timezone_utc(tz_kw.value), "fromtimestamp tz= must be timezone.utc"


def test_source_strptime_path_replaces_tzinfo_utc():
    tree = ast.parse(_VALIDATE_CHECKS_SRC)
    # every strptime call must be immediately wrapped in .replace(tzinfo=timezone.utc)
    wrapped = []
    for repl in _calls_named(tree, "replace"):
        tzinfo_kw = next((kw for kw in repl.keywords if kw.arg == "tzinfo"), None)
        if (
            tzinfo_kw is not None
            and _is_timezone_utc(tzinfo_kw.value)
            and isinstance(repl.func.value, ast.Call)
            and isinstance(repl.func.value.func, ast.Attribute)
            and repl.func.value.func.attr == "strptime"
        ):
            wrapped.append(repl)
    assert wrapped, "the strptime path must be followed by .replace(tzinfo=timezone.utc)"
    strptime_calls = list(_calls_named(tree, "strptime"))
    assert strptime_calls, "expected a strptime call"
    wrapped_strptimes = {id(w.func.value) for w in wrapped}
    for call in strptime_calls:
        assert id(call) in wrapped_strptimes, (
            "a naive strptime (not wrapped in .replace(tzinfo=timezone.utc)) remains"
        )


def _major_cluster_msg(findings):
    return next(
        (f.message for f in findings if f.severity == "major" and "orphaned tasks" in f.message),
        None,
    )


def test_int_epoch_orphans_cluster_on_utc_hour():
    # 2026-01-01T09:00:00Z == epoch 1767258000; three same-hour int-epoch orphans -> MAJOR.
    base = 1767258000  # 2026-01-01 09:00:00 UTC
    orphans = [_issue(f"e{i}", "open", parent=None, created_at=base + i * 60) for i in range(3)]
    findings = vc.check_orphaned_tasks(orphans)
    msg = _major_cluster_msg(findings)
    assert msg is not None, "3 same-UTC-hour int-epoch orphans must fire the MAJOR cluster"
    assert "2026-01-01 09:00" in msg, msg


def test_string_timestamp_orphans_cluster_on_utc_hour():
    orphans = [
        _issue(f"s{i}", "open", parent=None, created_at=f"2026-01-01T09:{i:02d}:00")
        for i in range(3)
    ]
    findings = vc.check_orphaned_tasks(orphans)
    msg = _major_cluster_msg(findings)
    assert msg is not None, "3 same-UTC-hour string-timestamp orphans must fire the MAJOR cluster"
    assert "2026-01-01 09:00" in msg, msg


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset unavailable on this platform")
def test_int_epoch_cluster_hour_is_tz_invariant():
    base = 1767258000  # 2026-01-01 09:00:00 UTC
    orphans = [_issue(f"e{i}", "open", parent=None, created_at=base + i * 60) for i in range(3)]
    original_tz = os.environ.get("TZ")

    def cluster_hour():
        return _major_cluster_msg(vc.check_orphaned_tasks(orphans))

    try:
        os.environ["TZ"] = "UTC"
        time.tzset()
        utc_msg = cluster_hour()

        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        la_msg = cluster_hour()
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()

    assert utc_msg is not None and la_msg is not None
    assert "2026-01-01 09:00" in utc_msg
    assert utc_msg == la_msg, "the UTC cluster hour must not depend on the host TZ"
