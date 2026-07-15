"""Story ff6f (depressant-amethyst-wasp): the ``/ticket/<id>`` audit page — HELD-OUT oracle.

Withheld from the implementation subagent (which sees only ``test_audit_ticket_page.py``).
These assert the contracted, per-AC behaviour of the finding-centric audit page against
the DOM markers the plan pins (``conv-bars`` / ``conv-bar`` / ``conv-line`` / ``polyline``,
the sticky gate strip's in-page anchors, decision-grouped ``<details>``) plus observable
rendered content.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")  # the [ui] extra; absent in the lean CI suite
pytest.importorskip("httpx")

import rebar  # noqa: E402
from tests.unit import _audit_page_helpers as H  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return H.make_store(tmp_path, monkeypatch)


def _get(repo: str, path: str):
    from starlette.testclient import TestClient

    from rebar.audit import server

    return TestClient(server.create_app(repo_root=repo)).get(path)


def _new_ticket(repo: str, title: str = "work") -> str:
    return rebar.create_ticket("task", title, description="x" * 60, repo_root=repo)


# ── AC1: gate strip — all three verdicts, counts, in-page anchors ────────────
def test_gate_strip_shows_all_three_states_counts_and_anchors(store: Path) -> None:
    r = str(store)
    tid = _new_ticket(r)
    # plan PASS, 0 blocking
    H.emit_plan_round(
        r,
        tid,
        verdict="PASS",
        findings=[
            H.finding("a1", decision="advisory", finding="nit", priority=0.2, block_threshold=0.9)
        ],
    )
    # completion PASS with 1/2 criteria met
    H.emit_completion_pass(
        r,
        tid,
        [
            {
                "criterion": "c1",
                "met": True,
                "kind": "codebase-verifiable",
                "citation": "src/x.py:1",
            },
            {"criterion": "c2", "met": False, "kind": "codebase-verifiable", "citation": None},
        ],
    )
    # no code review
    body = _get(r, f"/ticket/{tid}").text
    low = body.lower()
    # all three gates named with their state/counts
    assert "plan" in low and "completion" in low and "code" in low
    assert re.search(r"1\s*/\s*2", body), "completion met-count 1/2 not shown"
    assert "not run" in low or "not yet run" in low, "CODE 'not run' state missing"
    # each strip chip is an in-page anchor whose target id exists on the page
    for anchor in re.findall(r'href="#([\w-]+)"', body):
        assert f'id="{anchor}"' in body, f"anchor #{anchor} has no target element"
    assert len(re.findall(r'href="#[\w-]+"', body)) >= 3, "fewer than 3 in-page anchors"


# ── AC2: detail sections in fixed order plan → completion → code ─────────────
@pytest.mark.parametrize("fail_gate", ["plan", "completion"])
def test_sections_render_in_fixed_order(store: Path, fail_gate: str) -> None:
    r = str(store)
    tid = _new_ticket(r, f"order-{fail_gate}")
    H.emit_plan_round(
        r,
        tid,
        verdict=("FAIL" if fail_gate == "plan" else "PASS"),
        findings=[
            H.finding(
                "b1",
                decision="block" if fail_gate == "plan" else "advisory",
                finding="f",
                priority=0.9,
                block_threshold=0.5,
            )
        ],
    )
    if fail_gate == "completion":
        H.emit_completion_fail(r, tid, [{"criterion": "c", "met": False}])
    else:
        H.emit_completion_pass(
            r,
            tid,
            [{"criterion": "c", "met": True, "kind": "codebase-verifiable", "citation": "x"}],
        )
    H.emit_code_review(r, tid, advisory=[{"finding": "cr", "location": "y"}])
    low = _get(r, f"/ticket/{tid}").text.lower()
    p, c, k = low.find("plan"), low.rfind("completion"), low.rfind("code")
    # find the SECTION anchors specifically (id= markers), robust to header text
    ip = low.find('id="gate-plan"') if 'id="gate-plan"' in low else p
    ic = low.find('id="gate-completion"') if 'id="gate-completion"' in low else c
    ik = low.find('id="gate-code"') if 'id="gate-code"' in low else k
    assert ip < ic < ik, f"sections out of order: plan={ip} completion={ic} code={ik}"


# ── AC3: finding expands to four-pass provenance with SPECIFIC values ────────
def test_plan_finding_expands_to_four_pass_values(store: Path) -> None:
    r = str(store)
    tid = _new_ticket(r)
    H.emit_plan_round(
        r,
        tid,
        findings=[
            H.finding(
                "b1",
                decision="block",
                finding="Retry loop is unbounded",
                priority=0.9,
                block_threshold=0.6,
                evidence=("no ceiling in the plan text",),
                reason="the loop has no exit condition",
                verification={
                    "binary": {"is_real": "yes"},
                    "severity_attributes": {"blast_radius": "system"},
                },
            )
        ],
        coaching=[
            {
                "move_id": "m1",
                "finding_refs": ["b1"],
                "move_name": "add-cap",
                "subject": "retry",
                "coaching": "bound the retry budget",
            }
        ],
    )
    body = _get(r, f"/ticket/{tid}").text
    # pass-1 finding+evidence, pass-2 verification, pass-3 decision+reason, pass-4 coaching
    assert "Retry loop is unbounded" in body
    assert "no ceiling in the plan text" in body
    assert "the loop has no exit condition" in body  # pass-3 reason
    assert "system" in body or "is_real" in body  # pass-2 verification content
    assert "bound the retry budget" in body  # pass-4 coaching


# ── AC4: threshold meter — distinct ticks per finding + v1 fallback ──────────
def test_threshold_meter_distinct_ticks_per_finding(store: Path) -> None:
    r = str(store)
    tid = _new_ticket(r)
    H.emit_plan_round(
        r,
        tid,
        findings=[
            H.finding("b1", decision="block", finding="one", priority=0.8, block_threshold=0.5),
            H.finding("b2", decision="block", finding="two", priority=0.8, block_threshold=0.95),
        ],
    )
    body = _get(r, f"/ticket/{tid}").text
    # the two findings' ticks are at their OWN thresholds → two distinct position values.
    positions = set(re.findall(r'(?:left|--tick|x1|x2)\s*[:=]\s*"?(\d{1,3}(?:\.\d+)?)%?', body))
    assert len(positions) >= 2, f"expected >=2 distinct tick positions, got {positions}"


def test_threshold_meter_v1_fallback_when_threshold_absent(store: Path) -> None:
    r = str(store)
    tid = _new_ticket(r)
    f = H.finding("b1", decision="block", finding="legacy", priority=0.8, block_threshold=0.5)
    f["block_threshold"] = None  # simulate a v1 sidecar finding lacking the boundary
    H.emit_plan_round(r, tid, findings=[f])
    body = _get(r, f"/ticket/{tid}").text.lower()
    assert "legacy" in body
    assert "not recorded" in body or "threshold n/a" in body or "not available" in body


# ── AC5: findings grouped by decision series ─────────────────────────────────
def test_findings_grouped_by_decision_blocking_expanded(store: Path) -> None:
    r = str(store)
    tid = _new_ticket(r)
    H.emit_plan_round(
        r,
        tid,
        findings=[
            H.finding(
                "b1", decision="block", finding="BLOCKER-HI", priority=0.9, block_threshold=0.5
            ),
            H.finding(
                "b2", decision="block", finding="BLOCKER-LO", priority=0.4, block_threshold=0.5
            ),
            H.finding(
                "a1", decision="advisory", finding="ADV-ONE", priority=0.3, block_threshold=0.9
            ),
            H.finding(
                "d1", decision="dropped", finding="DROP-ONE", priority=0.1, block_threshold=0.9
            ),
        ],
    )
    body = _get(r, f"/ticket/{tid}").text
    low = body.lower()
    # blocking group is open; advisory/dropped groups collapsed with counts
    assert "blocking" in low
    # within blocking, higher-priority finding sorts first
    assert body.find("BLOCKER-HI") < body.find("BLOCKER-LO")
    # advisory/dropped shown as counted groups
    assert re.search(r"advisory[^<]{0,20}\b1\b", low) or "advisory (1)" in low
    assert re.search(r"dropped[^<]{0,20}\b1\b", low) or "dropped (1)" in low


# ── AC6: convergence viz — bars (<=3 rounds) vs line (>=4 rounds) + table ────
def test_convergence_two_round_bars(store: Path) -> None:
    r = str(store)
    tid = _new_ticket(r)
    for i in range(2):
        H.emit_plan_round(
            r,
            tid,
            material=f"m{i}",
            findings=[
                H.finding(
                    f"b{i}", decision="block", finding=f"r{i}", priority=0.9, block_threshold=0.5
                )
            ],
        )
    body = _get(r, f"/ticket/{tid}").text
    assert 'class="conv-bars"' in body
    assert body.count('class="conv-bar"') == 2
    assert 'class="conv-line"' not in body
    assert "<table" in body  # visually-hidden screen-reader table


def test_convergence_four_round_line_with_polylines(store: Path) -> None:
    r = str(store)
    tid = _new_ticket(r)
    # 4 rounds but only TWO non-empty decision series (block + advisory). This distinguishes
    # "one polyline per SERIES" (correct → 2) from "one polyline per ROUND" (wrong → 4).
    for i in range(4):
        H.emit_plan_round(
            r,
            tid,
            material=f"m{i}",
            findings=[
                H.finding(
                    f"b{i}", decision="block", finding=f"b{i}", priority=0.9, block_threshold=0.5
                ),
                H.finding(
                    f"a{i}", decision="advisory", finding=f"a{i}", priority=0.3, block_threshold=0.9
                ),
            ],
        )
    body = _get(r, f"/ticket/{tid}").text
    assert 'class="conv-line"' in body
    assert 'class="conv-bars"' not in body
    # one polyline per NON-EMPTY series (2 populated → 2 polylines), NOT one per round (4)
    assert body.count("<polyline") == 2, (
        f"expected 2 polylines (per-series), got {body.count('<polyline')}"
    )
    assert "<table" in body


# ── AC7: round selector — default latest, older round renders distinct ───────
def test_round_selector_default_latest_and_older(store: Path) -> None:
    r = str(store)
    tid = _new_ticket(r)
    # older round first, then newer round (all_review_results is newest-first)
    H.emit_plan_round(
        r,
        tid,
        material="old",
        findings=[
            H.finding(
                "o1",
                decision="block",
                finding="OLD-ROUND-FINDING",
                priority=0.9,
                block_threshold=0.5,
            )
        ],
    )
    H.emit_plan_round(
        r,
        tid,
        material="new",
        findings=[
            H.finding(
                "n1",
                decision="block",
                finding="NEW-ROUND-FINDING",
                priority=0.9,
                block_threshold=0.5,
            )
        ],
    )
    default_body = _get(r, f"/ticket/{tid}").text
    assert "NEW-ROUND-FINDING" in default_body  # default = latest
    older_body = _get(r, f"/ticket/{tid}?plan_round=2").text
    assert "OLD-ROUND-FINDING" in older_body  # round 2 (1-based, newest=1) = the older round
    assert "NEW-ROUND-FINDING" not in older_body or older_body != default_body


def test_code_round_selector_default_latest_and_older(store: Path) -> None:
    """The retained CODE-review history is navigable too: seed >=2 code-review rounds on
    one artifact and assert ?code_round=2 renders the older round's distinct content."""
    r = str(store)
    tid = _new_ticket(r)
    # give the ticket a plan review too, so the page is a normal multi-gate page
    H.emit_plan_round(
        r,
        tid,
        verdict="PASS",
        findings=[
            H.finding("a0", decision="advisory", finding="n", priority=0.2, block_threshold=0.9)
        ],
    )
    # older code round first, then newer (all_review_results is newest-first)
    cr = H.emit_code_review(
        r, tid, blocking=[{"finding": "OLD-CODE-FINDING", "location": "src/a.py:1"}]
    )
    H.emit_code_round(r, cr, blocking=[{"finding": "NEW-CODE-FINDING", "location": "src/b.py:2"}])
    default_body = _get(r, f"/ticket/{tid}").text
    assert "NEW-CODE-FINDING" in default_body  # default = latest code round
    older_body = _get(r, f"/ticket/{tid}?code_round=2").text
    assert "OLD-CODE-FINDING" in older_body  # round 2 = the older code round
    assert "NEW-CODE-FINDING" not in older_body or older_body != default_body


# ── AC8: completion panel — PASS criteria + operator-attested flag; FAIL ─────
def test_completion_pass_criteria_and_operator_attested_flag(store: Path) -> None:
    r = str(store)
    tid = _new_ticket(r)
    H.emit_plan_round(
        r,
        tid,
        verdict="PASS",
        findings=[
            H.finding("a1", decision="advisory", finding="n", priority=0.2, block_threshold=0.9)
        ],
    )
    H.emit_completion_pass(
        r,
        tid,
        [
            {
                "criterion": "code criterion met",
                "met": True,
                "kind": "codebase-verifiable",
                "citation": "src/x.py:5",
            },
            {
                "criterion": "deploy attested",
                "met": False,
                "kind": "operator-attested",
                "citation": None,
            },
        ],
    )
    body = _get(r, f"/ticket/{tid}").text
    low = body.lower()
    assert "code criterion met" in body and "deploy attested" in body
    assert "src/x.py:5" in body  # citation rendered
    assert "operator-attested" in low and "codebase-verifiable" in low  # kind badges
    # the operator-attested criterion with met=False is flagged as lacking attestation
    assert "lacking" in low or "no attestation" in low or "missing" in low or "not attested" in low


def test_completion_fail_renders_failure_findings_fallback(store: Path) -> None:
    r = str(store)
    tid = _new_ticket(r)
    H.emit_plan_round(
        r,
        tid,
        verdict="PASS",
        findings=[
            H.finding("a1", decision="advisory", finding="n", priority=0.2, block_threshold=0.9)
        ],
    )
    H.emit_completion_fail(
        r, tid, [{"criterion": "must handle empty input", "met": False, "citation": "src/z.py:9"}]
    )
    body = _get(r, f"/ticket/{tid}").text
    low = body.lower()
    assert "fail" in low
    assert "must handle empty input" in body  # the failure finding is surfaced


# ── AC9: empty/partial state — plan-only ticket ──────────────────────────────
def test_plan_only_ticket_shows_not_run_panels(store: Path) -> None:
    r = str(store)
    tid = _new_ticket(r)
    H.emit_plan_round(
        r,
        tid,
        findings=[
            H.finding(
                "b1", decision="block", finding="the finding", priority=0.9, block_threshold=0.5
            )
        ],
    )
    body = _get(r, f"/ticket/{tid}").text
    low = body.lower()
    assert "the finding" in body  # plan section renders
    # completion + code render explicit "not yet run" panels (headings, never hidden)
    assert low.count("not yet run") + low.count("not run") >= 2
    assert "<h2" in low or "<h3" in low
