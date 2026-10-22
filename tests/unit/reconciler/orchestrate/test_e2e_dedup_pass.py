"""E2E test: dedup guard prevents duplicate Jira creation.

Integration test wiring differ → applier with a fake AcliClient for one
reconciler pass.  Canonical DD-3 evidence: pre-existing issue with
rebar-id label produces zero create_issue calls.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
DIFFER_PATH = SCRIPTS_DIR.parent / "reconciler" / "differ.py"
APPLIER_PATH = SCRIPTS_DIR.parent / "reconciler" / "applier.py"


@pytest.fixture(scope="module")
def differ():
    if not DIFFER_PATH.exists():
        pytest.fail(f"differ.py not found at {DIFFER_PATH}")
    return importlib.import_module("rebar.reconciler.differ")


@pytest.fixture(scope="module")
def applier():
    if not APPLIER_PATH.exists():
        pytest.fail(f"applier.py not found at {APPLIER_PATH}")
    return importlib.import_module("rebar.reconciler.applier")


# ---------------------------------------------------------------------------
# Fake AcliClient
# ---------------------------------------------------------------------------


class FakeAcliClient:
    """Simulates AcliClient with PROJ-999 pre-existing, labeled rebar-id:uuid-X."""

    def __init__(self):
        self.creates: list = []
        self.updates: list = []
        self.transitions: list = []

    def search_issues(self, jql: str) -> list:
        # Return the pre-existing issue only when searching for uuid-X's label
        if "uuid-X" in jql:
            return [{"key": "PROJ-999", "labels": ["rebar-id:uuid-X"]}]
        return []

    def create_issue(self, fields: dict) -> dict:
        self.creates.append(fields)
        return {"key": "PROJ-NEW"}

    def update_issue(self, key: str, **fields) -> dict:
        # F3: real signature is update_issue(jira_key, **kwargs); applier
        # unpacks fields as kwargs, so the stub must accept them that way.
        self.updates.append((key, fields))
        return {"key": key}

    def transition_issue(self, key: str, status: str) -> None:
        self.transitions.append((key, status))

    # Bug 85a1 / Gap 1+5+8: create_one + inbound_create + update_one now
    # dispatch identity writes, labels, comments, and unassign-via-REST.
    # The stub accepts these as no-ops so the test exercises the dedup path.
    def add_label(self, key: str, label: str) -> None:
        return None

    def remove_label(self, key: str, label: str) -> None:
        return None

    def add_comment(self, key: str, body: str) -> dict:
        return {"id": "stub-comment"}

    def set_entity_property(self, key: str, prop: str, value) -> None:
        return None

    def delete_issue(self, key: str) -> None:
        return None

    def unassign_issue(self, key: str) -> None:
        return None

    def transition_issue_by_name(self, key: str, target: str) -> None:
        return None


# ---------------------------------------------------------------------------
# Stable tickets-branch HEAD (avoids git subprocess calls in tmp_path)
# ---------------------------------------------------------------------------

_STABLE_SHA = "deadbeef" * 5  # 40-char stable HEAD for drift guard


def _fake_snapshot_head(_repo_root) -> str:
    """Stand-in for ``applier.snapshot_head`` with a stable HEAD."""
    return _STABLE_SHA


# ---------------------------------------------------------------------------
# E2E test
# ---------------------------------------------------------------------------


def test_pre_existing_rebar_id_produces_zero_creates(tmp_path, differ, applier):
    """A Jira issue already carrying a rebar-id:<local_id> label → zero creates (end-to-end).

    Production semantics since commit 1f0032df24 / bug 4354: the fetcher
    snapshot stores Jira ``fields`` only (never the ``local_id`` entity
    property), so the snapshot differ recognises an already-bound issue by its
    ``rebar-id:<local_id>`` / ``rebar-id-<local_id>`` label and STANDS DOWN — the
    issue is owned by the binding-aware inbound/outbound differs. No inbound
    CREATE is emitted, so apply() never materialises a phantom ``jira-dig-NNNN``
    local ticket and never writes a ghost ``rebar-id:`` label back to Jira.

    This replaces an obsolete pre-4354 contract that asserted an applier-level
    dedup guard (mapping.json + a ``dedup-create-skipped`` manifest event). That
    guard never existed on the inbound-create path, and is unnecessary: dedup of
    already-bound issues is the differ's job, exercised here end-to-end
    (differ → applier). The earlier failing assertion (``mapping.json must be
    written by the dedup guard``) wired an *inbound* create yet asserted
    *outbound* dedup artifacts; the ``get_comments`` AttributeError it cited was
    a caught symptom, not the cause. Regression history: bugs a666/b38a/38fd/4cc1.
    """
    fake_client = FakeAcliClient()

    # S4: _load_acli returns the transport (client) directly.

    def _mut_action(m):
        a = getattr(m, "action", None)
        if a is not None:
            return getattr(a, "value", a)
        return m.get("action") if isinstance(m, dict) else None

    def _mut_key(m):
        return getattr(m, "target", None) or (m.get("key") if isinstance(m, dict) else None)

    # Step 1 — differ: a bound Jira issue (carries a rebar-id:<local_id> label)
    # present in the Jira snapshot but absent from the local snapshot must NOT
    # produce an inbound create — the 4354 label stand-down.
    prev_snapshot: dict = {}
    next_snapshot: dict = {
        "DIG-999": {
            "summary": "Already mirrored",
            "status": "open",
            "labels": ["rebar-id:jira-dig-999"],
        }
    }
    mutations = differ.compute_mutations(prev_snapshot, next_snapshot)

    create_mutations = [
        m for m in mutations if _mut_action(m) == "create" and _mut_key(m) == "DIG-999"
    ]
    assert not create_mutations, (
        "differ must stand down for a label-bound issue (bug 4354); "
        f"got a create mutation: {mutations}"
    )

    # Step 2 — apply the (create-free) mutation set: must be a no-op on Jira
    # (no phantom create, no ghost label write-back).
    with (
        patch.object(applier, "_load_acli", return_value=fake_client),
        patch.object(applier, "snapshot_head", _fake_snapshot_head),
    ):
        applier.apply(mutations, "pass-001", repo_root=tmp_path)

    assert fake_client.creates == [], "a label-bound issue must never trigger create_issue"

    # Step 3 — regression guard: a genuinely-unbound Jira issue (no rebar-id
    # label) STILL produces an inbound create. The stand-down is scoped to
    # bound issues; without this, 4354 would over-suppress legitimate creates.
    unbound_snapshot: dict = {"DIG-888": {"summary": "Brand new", "status": "open", "labels": []}}
    unbound_mutations = differ.compute_mutations({}, unbound_snapshot)
    unbound_creates = [
        m for m in unbound_mutations if _mut_action(m) == "create" and _mut_key(m) == "DIG-888"
    ]
    assert unbound_creates, (
        "a genuinely-unbound Jira issue must still produce an inbound create; "
        f"got {unbound_mutations}"
    )
