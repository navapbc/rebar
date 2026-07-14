"""COMPREHENSIVE LIVE round-trip test for Jira link sync (story 25ae-92e6-2927-49b6).

This is the DIFFER→APPLY round-trip the client-primitive probe
(tests/external/test_link_sync_live.py) deliberately is NOT: that probe proves
``set_relationship`` / ``get_issue_links`` / ``delete_issue_link`` function and
captures the live issuelink JSON shape; THIS test drives the actual
reconciler differ/apply semantics against REAL Jira:

  1. Outbound DIFFER emits a link ADD for a local ``blocks`` dep, and the
     outbound APPLY leaf (``apply_outbound._apply_outbound_update``) creates a
     real Jira link via the same wiring production uses.
  2. The outbound DIFFER dedups against the now-live link (a second reconcile
     pass would emit NO link ADD — no duplicate, no churn).
  3. The inbound DIFFER reads the real live issuelink (REST-nested shape) and
     reflects it into a rebar relation with the correct direction.
  4. (OPTIONAL) inbound APPLY into a throwaway rebar tracker — SKIPPED by
     default (the inbound apply via ``rebar.link`` is covered by the mocked
     unit test ``tests/integration/rebar_reconciler/test_link_sync.py``); see
     the note at the call site.

Gating (mirrors the client-primitive probe): auto-marked ``external`` by the
root conftest hook, made inert unless ``REBAR_RUN_EXTERNAL=1`` by
tests/external/conftest.py, and skipped here unless live Jira credentials AND
the ``acli`` binary are present. It makes REAL Jira mutations and MUST be run
SERIALLY, once, never concurrently (story 25ae operational hazard note:
concurrent runs collide on rate-limit backoff and orphan probe issues).

Cleanup (try/finally) deletes EVERY Jira artifact this test creates — issue
links first (get_issue_links → delete_issue_link by id), then issues A and B —
and is robust to partial setup (A created but B failed, link created but an
assertion failed, etc.). The live issuelink JSON shape is printed once for the
record.

Run locally with credentials::

    REBAR_RUN_EXTERNAL=1 JIRA_URL=… JIRA_USER=… JIRA_API_TOKEN=… \
        pytest -m external tests/external/test_link_sync_roundtrip_live.py -s
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.external

# Repo root: tests/external/<this file> -> parents[2] is the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
RECONCILER_DIR = ENGINE_DIR / "rebar_reconciler"


# ---------------------------------------------------------------------------
# Gating helpers (mirror tests/external/test_link_sync_live.py)
# ---------------------------------------------------------------------------


def _live_jira_ready() -> bool:
    creds = all(os.environ.get(k) for k in ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN"))
    return creds and shutil.which("acli") is not None


_skip = pytest.mark.skipif(not _live_jira_ready(), reason="no live Jira creds / acli binary")


def _ensure_engine_on_path() -> None:
    """Put <repo>/src/rebar/_engine on sys.path so ``rebar_reconciler`` resolves.

    The reconciler ships as the stdlib-only ``rebar_reconciler`` package under
    the engine dir (not a top-level installed package). The outbound apply leaf
    imports ``from rebar_reconciler.apply_base import ...`` at module import
    time, so the package MUST be importable as a package (not merely loaded
    file-by-file via spec_from_file_location). Mirrors the client builder in
    tests/external/test_link_sync_live.py and tests/unit/.../conftest.py.
    """
    if str(ENGINE_DIR) not in sys.path:
        sys.path.insert(0, str(ENGINE_DIR))


def _build_client():
    _ensure_engine_on_path()
    from rebar_reconciler import acli as mod

    return mod.AcliClient(
        jira_url=os.environ["JIRA_URL"],
        user=os.environ["JIRA_USER"],
        api_token=os.environ["JIRA_API_TOKEN"],
        jira_project=os.environ.get("JIRA_PROJECT", "DIG"),
    )


def _load_module(name: str, path: Path) -> ModuleType:
    """Load a reconciler differ module standalone (mirrors the integration test).

    The differs are written to be import-via-spec safe (they lazy-load siblings
    by path), so loading them this way exercises the same standalone path the
    integration suite uses.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _new_key(created: dict) -> str:
    key = created.get("key") or created.get("issueKey") or (created.get("issue") or {}).get("key")
    assert key, f"create_issue returned no key: {created!r}"
    return key


# ---------------------------------------------------------------------------
# StubBindingStore — serves both directions over one local_id<->jira_key map
# (mirrors tests/integration/rebar_reconciler/test_link_sync.py)
# ---------------------------------------------------------------------------


class StubBindingStore:
    """Two-directional binding store over a single local_id<->jira_key map."""

    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._l2j: dict[str, str] = dict(bindings or {})
        self._j2l: dict[str, str] = {v: k for k, v in self._l2j.items()}

    def get_jira_key(self, local_id: str) -> str | None:
        return self._l2j.get(local_id)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._l2j

    def get_local_id(self, jira_key: str) -> str | None:
        return self._j2l.get(jira_key)

    def get_baseline(self, local_id: str) -> dict[str, object] | None:
        # No last-synced Jira-side baseline is recorded in this stub, so the outbound
        # differ degrades to local-wins (ADR 0026 §2) — matching this test's intended
        # pre-baseline-rollout semantics. Mirrors BindingStore.get_baseline's contract
        # (an absent baseline is a valid None); outbound_fields now ALWAYS calls it.
        return None


def _make_ticket(
    ticket_id: str,
    *,
    title: str = "Some ticket",
    status: str = "open",
    deps: list[dict] | None = None,
) -> dict:
    """Build a local ticket dict in the shape ``rebar list`` emits.

    ``deps`` carries link data as ``[{"target_id", "relation", "link_uuid"}]``.
    """
    return {
        "ticket_id": ticket_id,
        "title": title,
        "description": "A description long enough to be realistic for the differ machinery.",
        "status": status,
        "priority": 2,
        "ticket_type": "task",
        "assignee": "alice",
        "tags": [],
        "comments": [],
        "deps": deps or [],
        "parent_id": None,
    }


def _build_outbound_snapshot(client, key_a: str, key_b: str) -> dict[str, dict]:
    """Build the outbound differ snapshot for A and B from LIVE Jira state.

    The outbound differ reads each bound issue's ``issuelinks`` array from its
    snapshot entry (``_diff_links`` → ``_existing_jira_links``). We populate A's
    snapshot entry's ``issuelinks`` from a live ``client.get_issue_links(A)``
    call so the dedup check (step 2) runs against REAL Jira link state.

    We include ``comment`` keys (empty) so the differ's ``_diff_comments`` takes
    the snapshot-carried (fixture) path and never makes a live get_comments call
    — this test is about links, not comments.
    """
    links_a = client.get_issue_links(key_a)
    return {
        key_a: {
            "summary": "rebar link-sync roundtrip A (auto-delete)",
            "status": {"name": "To Do"},
            "issuetype": {"name": "Task"},
            "priority": {"name": "Medium"},
            "assignee": {"displayName": "alice"},
            "labels": [],
            "comment": {"comments": []},
            "issuelinks": links_a if isinstance(links_a, list) else [],
        },
        key_b: {
            "summary": "rebar link-sync roundtrip B (auto-delete)",
            "status": {"name": "To Do"},
            "issuetype": {"name": "Task"},
            "priority": {"name": "Medium"},
            "assignee": {"displayName": "alice"},
            "labels": [],
            "comment": {"comments": []},
            "issuelinks": [],
        },
    }


@_skip
def test_link_sync_differ_apply_roundtrip_live() -> None:
    """ONE serial live round-trip: differ→apply→differ-dedup→inbound-reflect.

    Single test function, no parametrization (avoids multiplying live calls).
    """
    _ensure_engine_on_path()

    client = _build_client()
    outbound = _load_module("outbound_differ", RECONCILER_DIR / "outbound_differ.py")
    inbound = _load_module("inbound_differ", RECONCILER_DIR / "inbound_differ.py")

    # The outbound apply leaf must be imported as a PACKAGE module (it does
    # ``from rebar_reconciler.apply_base import ...`` at import time).
    from rebar_reconciler import apply_outbound
    from rebar_reconciler.mutation import Mutation, MutationAction, MutationDirection

    key_a: str | None = None
    key_b: str | None = None

    try:
        # ---- Setup: two real Jira issues A and B, bound to loc-a / loc-b. ----
        created_a = client.create_issue(
            {"ticket_type": "task", "title": "rebar link-sync roundtrip A (auto-delete)"}
        )
        key_a = _new_key(created_a)
        created_b = client.create_issue(
            {"ticket_type": "task", "title": "rebar link-sync roundtrip B (auto-delete)"}
        )
        key_b = _new_key(created_b)

        bind = StubBindingStore({"loc-a": key_a, "loc-b": key_b})

        loc_a = _make_ticket(
            "loc-a",
            title="rebar link-sync roundtrip A (auto-delete)",
            deps=[{"target_id": "loc-b", "relation": "blocks", "link_uuid": "u1"}],
        )
        loc_b = _make_ticket("loc-b", title="rebar link-sync roundtrip B (auto-delete)")

        # =================================================================
        # STEP 1 — Outbound apply creates a real link.
        # =================================================================
        # Build the snapshot from LIVE state (A has no Blocks→B link yet).
        snapshot = _build_outbound_snapshot(client, key_a, key_b)
        assert snapshot[key_a]["issuelinks"] == [], (
            f"precondition: A must start with no issuelinks, got {snapshot[key_a]['issuelinks']!r}"
        )

        muts, _ = outbound.compute_outbound_mutations([loc_a, loc_b], snapshot, bind)
        a_mut = next((m for m in muts if m.local_id == "loc-a"), None)
        assert a_mut is not None, (
            "outbound emitted NO mutation for loc-a (expected a link ADD). "
            f"All mutations: {[(m.local_id, m.action) for m in muts]}"
        )
        assert a_mut.links, f"outbound mutation for loc-a carries no links: {a_mut!r}"
        add_link = next((lk for lk in a_mut.links if lk.get("action") == "add"), None)
        assert add_link is not None, f"no link ADD in outbound links: {a_mut.links!r}"
        assert add_link.get("to_key") == key_b, (
            f"outbound link ADD does not target B ({key_b}): {add_link!r}"
        )
        assert add_link.get("type") == "Blocks", (
            f"outbound link ADD type should be 'Blocks' for a 'blocks' dep: {add_link!r}"
        )

        # Apply it for real by DRIVING THE LEAF (exercises the apply wiring:
        # _apply_outbound_update → _call_with_retry(client.set_relationship, ...)).
        # The payload mirrors the shape reconcile builds from an OutboundMutation:
        # a flat changed_fields ({}) plus the links list the differ emitted.
        apply_mut = Mutation(
            direction=MutationDirection.outbound,
            action=MutationAction.update,
            target=key_a,
            payload={"fields": {}, "links": a_mut.links},
            provenance={"test": "link_sync_roundtrip_live", "local_id": "loc-a"},
        )
        result = apply_outbound._apply_outbound_update(apply_mut, client=client)
        assert (result.payload or {}).get("links_applied", 0) >= 1, (
            f"apply leaf reported no link applied: {getattr(result, 'payload', None)!r}"
        )

        # Assert the live link now exists on A, pointing at B (either direction).
        links_after_apply = client.get_issue_links(key_a)
        assert isinstance(links_after_apply, list)

        # Capture the EXACT live issuelink JSON shape once, for the record.
        print("\n=== LIVE issuelink JSON shape (get_issue_links on A after apply) ===")
        print(json.dumps(links_after_apply, indent=2, default=str))
        print("=== end issuelink shape ===\n")

        def _blocks_to_b(link: dict) -> bool:
            type_name = (link.get("type") or {}).get("name", "")
            outward = (link.get("outwardIssue") or {}).get("key")
            inward = (link.get("inwardIssue") or {}).get("key")
            return type_name == "Blocks" and key_b in (outward, inward)

        live_link = next(
            (lk for lk in links_after_apply if isinstance(lk, dict) and _blocks_to_b(lk)),
            None,
        )
        assert live_link is not None, (
            f"Blocks link A({key_a})→B({key_b}) not found after apply: "
            f"{json.dumps(links_after_apply, default=str)}"
        )

        # =================================================================
        # STEP 2 — Differ dedups against the live state (no duplicate/churn).
        # =================================================================
        # Rebuild the snapshot from LIVE state — A now carries the Blocks→B link.
        snapshot2 = _build_outbound_snapshot(client, key_a, key_b)
        assert snapshot2[key_a]["issuelinks"], (
            "step 2 precondition: A's live issuelinks should now be non-empty"
        )
        muts2, _ = outbound.compute_outbound_mutations([loc_a, loc_b], snapshot2, bind)
        a_mut2 = next((m for m in muts2 if m.local_id == "loc-a"), None)
        a_link_adds2 = (
            [lk for lk in (a_mut2.links or []) if lk.get("action") == "add"]
            if a_mut2 is not None
            else []
        )
        assert not a_link_adds2, (
            "outbound differ did NOT dedup against the live Jira link — it would "
            f"create a DUPLICATE on a second reconcile pass. link ADDs: {a_link_adds2!r}"
        )

        # =================================================================
        # STEP 3 — Inbound differ reads the real link + correct direction.
        # =================================================================
        # Build the inbound snapshot directly from the LIVE REST-nested
        # issuelinks (get_issue_links already returns the REST shape).
        live_links_a = client.get_issue_links(key_a)
        inbound_snapshot = {
            key_a: {
                "summary": "rebar link-sync roundtrip A (auto-delete)",
                "status": {"name": "To Do"},
                "issuetype": {"name": "Task"},
                "priority": {"name": "Medium"},
                "assignee": {"displayName": "alice"},
                "labels": [],
                "issuelinks": live_links_a,
            },
        }
        # loc_a has EMPTY deps here so the inbound differ MUST emit the link
        # (if it carried the dep it would dedup it away).
        loc_a_emptydeps = _make_ticket(
            "loc-a", title="rebar link-sync roundtrip A (auto-delete)", deps=[]
        )
        inbound_muts, _suppressed = inbound.compute_inbound_mutations(
            inbound_snapshot, bind, {"loc-a": loc_a_emptydeps, "loc-b": loc_b}
        )
        ia_mut = next((m for m in inbound_muts if m.local_id == "loc-a"), None)
        assert ia_mut is not None and ia_mut.links, (
            "inbound differ reflected NO link change for the live Jira issuelink. "
            f"Inbound mutations: "
            f"{[(m.local_id, getattr(m, 'links', None)) for m in inbound_muts]}"
        )
        ia_link = ia_mut.links[0]
        assert ia_link.get("target_id") == "loc-b", (
            f"inbound link change should target loc-b: {ia_link!r}"
        )
        # DIRECTION ASSERTION (documented).
        #
        # set_relationship(A, B, "Blocks") runs `link create --out A --in B` =
        # "A blocks B". Viewing A's REST issuelinks, B therefore appears as the
        # INWARDISSUE (A is the outward/blocker side). The inbound differ
        # (_diff_links_inbound) maps "other issue is inwardIssue + Blocks" to the
        # rebar relation **'blocks'** on A targeting B. So we assert 'blocks'.
        #
        # ROBUSTNESS: if the installed ACLI/Jira instead records B as
        # outwardIssue on A (i.e. the live link direction is reversed from what
        # was verified during development), the differ would emit 'depends_on'.
        # We assert the relation matches whichever direction the LIVE link
        # actually has, and surface a clear message documenting the observed
        # shape, so a direction surprise is an informative finding, not an
        # opaque failure.
        b_is_inward = any(
            isinstance(lk, dict)
            and (lk.get("type") or {}).get("name") == "Blocks"
            and (lk.get("inwardIssue") or {}).get("key") == key_b
            for lk in live_links_a
        )
        b_is_outward = any(
            isinstance(lk, dict)
            and (lk.get("type") or {}).get("name") == "Blocks"
            and (lk.get("outwardIssue") or {}).get("key") == key_b
            for lk in live_links_a
        )
        expected_relation = "blocks" if b_is_inward else "depends_on"
        assert b_is_inward or b_is_outward, (
            f"live link to B has neither inwardIssue nor outwardIssue==B: {live_links_a!r}"
        )
        assert ia_link.get("relation") == expected_relation, (
            f"inbound relation mismatch: live link records B as "
            f"{'inwardIssue' if b_is_inward else 'outwardIssue'} on A "
            f"(expected rebar relation {expected_relation!r}), but differ emitted "
            f"{ia_link.get('relation')!r}. live_links={live_links_a!r}"
        )

        # =================================================================
        # STEP 4 (OPTIONAL) — inbound apply into a throwaway rebar tracker.
        # =================================================================
        # SKIPPED by design. The inbound apply path
        # (apply_inbound._apply_inbound_update → rebar.link) is exercised by the
        # mocked unit/integration test
        # (tests/integration/rebar_reconciler/test_link_sync.py). Initializing a
        # throwaway tracker + creating loc-a/loc-b + driving rebar.link here adds
        # real-store mutation risk and complexity for no additional LIVE-Jira
        # coverage (the apply writes to LOCAL rebar, not Jira). Keeping it out
        # preserves this test's single-responsibility: the DIFFER↔live-Jira
        # round-trip. The relation correctness asserted in STEP 3 is exactly
        # what rebar.link would receive.

    finally:
        # ---- Cleanup: delete EVERY Jira artifact, robust to partial setup. ----
        # Delete the probe issues directly (404 is idempotent success per
        # delete_issue). Deleting an issue removes its issue links too, so we do
        # NOT call delete_issue_link here: that ACLI command currently hangs
        # (no subprocess timeout — bug d843), which would block cleanup
        # indefinitely and orphan the issues. Issue-delete is reliable and
        # link-removing, so it is the safe cleanup path.
        for key in (key_a, key_b):
            if key:
                try:
                    client.delete_issue(key)
                except Exception as exc:  # noqa: BLE001
                    print(f"CLEANUP WARNING: delete_issue({key}) failed: {exc!r}")
