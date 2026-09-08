"""External Jira link round trip through the reconciler differ and apply.

The client-primitive probe covers individual operations. This test drives the reconciler's
linked differ and apply path against Jira.

It applies a local ``blocks`` dependency, confirms the service link, verifies outbound dedup,
and maps the nested inbound response back to the correct local direction. Inbound application
to a local tracker remains covered by integration tests.

The serial test requires ``REBAR_RUN_EXTERNAL``, Jira connection settings including
``JIRA_PROJECT``, and ``acli``.

The ``finally`` cleanup removes every created artifact after partial setup or assertion failure.
The test also prints the Jira link shape for diagnostics.
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
    configured = all(
        os.environ.get(k) for k in ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN", "JIRA_PROJECT")
    )
    return configured and shutil.which("acli") is not None


_skip = pytest.mark.skipif(
    not _live_jira_ready(), reason="missing Jira connection configuration or acli binary"
)


def _ensure_engine_on_path() -> None:
    """Expose the engine directory so outbound apply can import ``rebar_reconciler``.

    The leaf imports ``rebar_reconciler.apply_base`` as a package, matching the other client
    fixtures.
    """
    if str(ENGINE_DIR) not in sys.path:
        sys.path.insert(0, str(ENGINE_DIR))


def _build_client():
    _ensure_engine_on_path()
    from rebar_reconciler.adapters.jira import acli as mod

    return mod.AcliClient(
        jira_url=os.environ["JIRA_URL"],
        user=os.environ["JIRA_USER"],
        api_token=os.environ["JIRA_API_TOKEN"],
        jira_project=os.environ["JIRA_PROJECT"],
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
    sys.modules[name] = mod
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

    def is_pending(self, local_id: str) -> bool:
        # All bindings in this stub are already resolved (constructed from a plain
        # local->jira map), so none are in the write-ahead "pending" state.
        return False

    def recover_pending_bindings(self, client: object, *, failure_sink: list | None = None) -> int:
        # No pending bindings to recover in this stub → zero recovered (story 9622).
        return 0


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
        # The typed leaf returns the production applier's batch result without per-link
        # telemetry. A clean result proves delegation. The Jira read below proves the link.
        assert result.payload and "update_result" in result.payload, (
            f"apply leaf did not delegate to update_one: {getattr(result, 'payload', None)!r}"
        )
        assert not result.payload.get("comment_errors"), (
            f"apply leaf reported comment errors: {result.payload.get('comment_errors')!r}"
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
        # Round-trip direction is absolute. ``set_relationship(A, B, "Blocks")`` records B as
        # A's outward issue, meaning A blocks B. Inbound resolution must reconstruct ``blocks``
        # on A. The mirror orientation reconstructs ``depends_on``.
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
        assert b_is_inward or b_is_outward, (
            f"live link to B has neither inwardIssue nor outwardIssue==B: {live_links_a!r}"
        )
        # A 'blocks' dep must record B as the OUTWARD side on A (A is the blocker).
        assert b_is_outward and not b_is_inward, (
            f"live direction surprise: an applied 'blocks' dep should record B as "
            f"outwardIssue on A, but got inward={b_is_inward} outward={b_is_outward}. "
            f"live_links={live_links_a!r}"
        )
        # Round-trip fidelity: the applied 'blocks' relation is reconstructed as 'blocks'.
        assert ia_link.get("relation") == "blocks", (
            f"link direction round-trip BROKE: applied a 'blocks' dep (B outward on A), "
            f"but the inbound differ reconstructed {ia_link.get('relation')!r} instead of "
            f"'blocks'. live_links={live_links_a!r}"
        )

        # Inbound application is intentionally omitted. Integration tests exercise
        # ``rebar.link``, while this external case isolates differ behavior against Jira and
        # avoids local tracker mutation.

    finally:
        # Delete issues directly because deletion also removes links and treats 404 as success.
        # Avoid ``delete_issue_link`` here because its CLI path can hang without a timeout.
        for key in (key_a, key_b):
            if key:
                try:
                    client.delete_issue(key)
                except Exception as exc:  # noqa: BLE001
                    print(f"CLEANUP WARNING: delete_issue({key}) failed: {exc!r}")


@_skip
@pytest.mark.parametrize(("relation", "a_is_blocker"), [("blocks", True), ("depends_on", False)])
def test_link_sync_writes_absolute_direction_live(relation: str, a_is_blocker: bool) -> None:
    """Verify the absolute blocker orientation for both local link relations.

    ``A blocks B`` places A on the outward side. ``A depends_on B`` places B on the outward
    side. Exercising ``batch_dispatch.update_one`` prevents differ and apply agreement from
    hiding a link written backward.
    """
    _ensure_engine_on_path()
    client = _build_client()
    outbound = _load_module("outbound_differ", RECONCILER_DIR / "outbound_differ.py")
    from rebar_reconciler.batch_dispatch import update_one

    key_a: str | None = None
    key_b: str | None = None
    try:
        key_a = _new_key(
            client.create_issue(
                {"ticket_type": "task", "title": f"rebar dir-{relation} A (auto-delete)"}
            )
        )
        key_b = _new_key(
            client.create_issue(
                {"ticket_type": "task", "title": f"rebar dir-{relation} B (auto-delete)"}
            )
        )
        bind = StubBindingStore({"loc-a": key_a, "loc-b": key_b})
        ticket = _make_ticket(
            "loc-a", deps=[{"target_id": "loc-b", "relation": relation, "link_uuid": "u1"}]
        )

        from rebar_reconciler.adapters.jira.backend import JiraBackend as _JB

        links = outbound._diff_links(
            ticket, {"issuelinks": client.get_issue_links(key_a)}, bind, _JB(transport=client)
        )
        assert links and links[0].get("type") == "Blocks", f"expected a Blocks ADD; got {links!r}"
        batch = {
            "action": "update",
            "direction": "outbound",
            "key": key_a,
            "fields": {},
            "local_id": "loc-a",
            "follow_on": None,
            "comments": [],
            "labels": [],
            "links": links,
        }
        update_one(batch, client)

        # Determine the ACTUAL blocker from A's live links: from A's perspective an
        # outwardIssue==B means "A blocks B"; an inwardIssue==B means "B blocks A".
        a_blocks_b = b_blocks_a = False
        live = client.get_issue_links(key_a)
        for lk in live:
            if (lk.get("type") or {}).get("name") != "Blocks":
                continue
            if (lk.get("outwardIssue") or {}).get("key") == key_b:
                a_blocks_b = True
            if (lk.get("inwardIssue") or {}).get("key") == key_b:
                b_blocks_a = True
        assert a_blocks_b ^ b_blocks_a, f"expected exactly one Blocks direction A↔B; got {live!r}"
        if a_is_blocker:
            assert a_blocks_b, (
                f"'A {relation} B' must write A as the blocker (A blocks B); got 'B blocks A'"
            )
        else:
            assert b_blocks_a, (
                f"'A {relation} B' must write B as the blocker (B blocks A); got 'A blocks B'"
            )
    finally:
        for key in (key_a, key_b):
            if key:
                try:
                    client.delete_issue(key)
                except Exception:  # noqa: BLE001 — cleanup best-effort
                    pass
