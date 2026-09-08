"""External end-to-end check of Jira ACLI link primitives.

The test requires the external opt-in, Jira configuration including ``JIRA_PROJECT``, and the
``acli`` binary. It creates a link where A blocks B, reads the Jira response shape, deletes the
link, and removes both issues in ``finally``. Integration tests cover reconcile wiring. This
module covers the client operations and service response.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

pytestmark = pytest.mark.external


def _live_jira_ready() -> bool:
    configured = all(
        os.environ.get(k) for k in ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN", "JIRA_PROJECT")
    )
    return configured and shutil.which("acli") is not None


_skip = pytest.mark.skipif(
    not _live_jira_ready(), reason="missing Jira connection configuration or acli binary"
)


def _build_client():
    # The reconciler ships as the stdlib-only ``rebar_reconciler`` package under
    # <repo>/src/rebar/_engine (not a top-level installed package). Put that dir
    # on sys.path so ``from rebar_reconciler import acli`` resolves, mirroring
    # tests/unit/rebar_reconciler/conftest.py.
    import sys
    from pathlib import Path

    engine_dir = Path(__file__).resolve().parents[2] / "src" / "rebar" / "_engine"
    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))

    from rebar_reconciler.adapters.jira import acli as mod

    return mod.AcliClient(
        jira_url=os.environ["JIRA_URL"],
        user=os.environ["JIRA_USER"],
        api_token=os.environ["JIRA_API_TOKEN"],
        jira_project=os.environ["JIRA_PROJECT"],
    )


def _new_key(created: dict) -> str:
    key = created.get("key") or created.get("issueKey") or (created.get("issue") or {}).get("key")
    assert key, f"create_issue returned no key: {created!r}"
    return key


@_skip
@pytest.mark.xfail(
    strict=False,
    reason="The link client primitives are broken against the live ACLI: set_relationship "
    "passes --json to `link create` (rejected by current ACLI), and get_issue_links parses "
    "the REST `issuelinks` shape while ACLI returns `{issueLinks:[{typeName,outwardIssueKey}]}`. "
    "TDD spec for story 25ae-92e6-2927-49b6; verified by 2b45-3c6f-0c13-442b. strict=False "
    "because the failure mode depends on the installed ACLI version.",
)
def test_link_primitives_roundtrip_live() -> None:
    client = _build_client()

    key_a: str | None = None
    key_b: str | None = None
    created_link_ids: list[str] = []

    try:
        created_a = client.create_issue(
            {"ticket_type": "task", "title": "rebar link-sync probe A (auto-delete)"}
        )
        key_a = _new_key(created_a)
        created_b = client.create_issue(
            {"ticket_type": "task", "title": "rebar link-sync probe B (auto-delete)"}
        )
        key_b = _new_key(created_b)

        set_result = client.set_relationship(key_a, key_b, "Blocks")
        assert set_result.get("status") == "created", f"set_relationship failed: {set_result!r}"

        links_a = client.get_issue_links(key_a)
        assert isinstance(links_a, list), f"get_issue_links did not return a list: {links_a!r}"

        # Print the Jira link shape for diagnostics.
        print("\n=== LIVE issuelink JSON shape (get_issue_links on A) ===")
        print(json.dumps(links_a, indent=2, default=str))
        print("=== end issuelink shape ===\n")

        def _matches_blocks_to_b(link: dict) -> bool:
            type_name = (link.get("type") or {}).get("name", "")
            outward = (link.get("outwardIssue") or {}).get("key")
            inward = (link.get("inwardIssue") or {}).get("key")
            return type_name == "Blocks" and key_b in (outward, inward)

        match = next(
            (lk for lk in links_a if isinstance(lk, dict) and _matches_blocks_to_b(lk)),
            None,
        )
        assert match is not None, (
            f"Blocks link A({key_a})->B({key_b}) not found in get_issue_links(A): "
            f"{json.dumps(links_a, default=str)}"
        )

        # Retain the ID for failure cleanup.
        link_id = match.get("id")
        assert link_id, f"matched issuelink carries no 'id' to delete by: {match!r}"
        created_link_ids.append(str(link_id))

        del_result = client.delete_issue_link(str(link_id))
        assert del_result.get("status") == "deleted", f"delete_issue_link failed: {del_result!r}"
        created_link_ids.clear()  # deleted successfully; nothing left to clean

        links_after = client.get_issue_links(key_a)
        still_present = any(isinstance(lk, dict) and _matches_blocks_to_b(lk) for lk in links_after)
        assert not still_present, (
            f"link still present after delete_issue_link({link_id}): "
            f"{json.dumps(links_after, default=str)}"
        )

    finally:
        # Clean up a link left by partial failure.
        for lid in created_link_ids:
            try:
                client.delete_issue_link(lid)
            except Exception as exc:  # noqa: BLE001
                print(f"CLEANUP WARNING: delete_issue_link({lid}) failed: {exc!r}")
        # Delete both issues. A 404 is idempotent success.
        for key in (key_a, key_b):
            if key:
                try:
                    client.delete_issue(key)
                except Exception as exc:  # noqa: BLE001
                    print(f"CLEANUP WARNING: delete_issue({key}) failed: {exc!r}")
