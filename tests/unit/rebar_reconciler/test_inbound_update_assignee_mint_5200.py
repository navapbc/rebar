"""The inbound UPDATE path never mints an identity, because it hands the mint a STRING.

Found by J11's inbound-assignee cell (ticket 5200) on harness run 30767546938 once its setup
was fixed and it reached its real assertion for the first time. This module localises the
mechanism harness-free, one variable at a time.

THE CHAIN, read end to end:

  1. ``inbound_fields._map_jira_to_local_fields:223-225`` maps the Jira user object to TWO
     canonical keys: the scalar ``assignee`` (a bare STRING via ``_extract_jira_field_value``,
     i.e. ``name`` or ``displayName``) and the fixed-shape ``assignee_identity``
     (``{display, email, account_id}``, where ``account_id`` already carries DC's username —
     ``_identity_of:154-179``).
  2. ``inbound_differ._diff_jira_vs_local:185-188`` emits ONLY the scalar:
     ``changed["assignee"] = jira_mapped.get("assignee")``. ``assignee_identity`` is not in
     ``field_map``, so it never reaches the mutation.
  3. ``apply_inbound_records._inbound_update_write_edit_event:333-340`` documents that these
     fields ARE the differ's local-keyed shape, and line 369 passes ``fields["assignee"]`` —
     that string — to ``_ensure_inbound_assignee_identity``.
  4. ``_ensure_inbound_assignee_identity:100-101`` opens with ``if not isinstance(assignee,
     dict): return``. A string returns immediately. NO MINT, and the caller cannot tell:
     the function is best-effort and silent by contract.

The CREATE path is unaffected and that is what has masked this: ``:200-203`` passes the RAW
Jira user object from the snapshot, which IS a dict, so an inbound CREATE mints correctly.
Every identity the harness has ever observed came from that path.

NOT A REGRESSION OF 5f48. That bug fixed the KEY SELECTION inside the mint (``accountId``
first, then DC's ``name``) so DC users would resolve. This defect is upstream of that guard:
on the update path the function returns before any key is read, so 5f48's fix is never
reached. It is a case 5f48 never covered.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import rebar
import rebar_reconciler.apply_inbound_records as air
from rebar_reconciler.inbound_differ import _diff_jira_vs_local

_DC_USER = "admin"
# The raw Jira Data Center user object: NO accountId, identified by `name` (bug 5f48's shape).
_RAW_DC_ASSIGNEE: dict[str, Any] = {
    "name": _DC_USER,
    "displayName": "Administrator",
    "emailAddress": "admin@example.com",
}


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "d@e.com"),
        ("git", "config", "user.name", "d"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


class _DCMapper:
    """The DC backend's own mapper, injected so no config/backend lookup is needed.

    `adapters/jira_datacenter/backend.py:116-119` delegates `map_remote_to_local` straight to
    `inbound_fields._map_jira_to_local_fields`, so this is that backend's real behaviour and
    not a stand-in for it.
    """

    def map_remote_to_local(self, remote_fields: dict[str, Any]) -> dict[str, Any]:
        from rebar_reconciler import inbound_fields

        return inbound_fields._map_jira_to_local_fields(remote_fields)


def test_the_differ_emits_the_assignee_as_a_string_not_the_user_object() -> None:
    """LINK 1-2. A newly-assigned issue produces an assignee change — and it is a STRING.

    The local ticket is UNASSIGNED and Jira reports the admin, which is exactly the state J11's
    cell arranges. So the differ does emit the field (the mint is not being skipped for want of
    a change), and what it emits is the scalar, not the user object the mint requires.
    """
    changed = _diff_jira_vs_local(
        {"assignee": dict(_RAW_DC_ASSIGNEE)},
        {"assignee": "", "title": "t", "description": "", "status": "open"},
        inbound_mapper=_DCMapper(),
    )

    assert "assignee" in changed, (
        "the differ reported NO assignee change for an unassigned local ticket whose Jira issue "
        "is assigned — if this is ever true, the live cell's setup, not the mint, is at fault"
    )
    assert isinstance(changed["assignee"], str), (
        f"the differ emitted {type(changed['assignee']).__name__} for assignee; this test's "
        f"premise (that the update path carries a scalar) no longer holds"
    )
    assert not isinstance(changed["assignee"], dict), "assignee reached the applier as a dict"


def test_the_mint_is_a_no_op_on_the_shape_the_update_path_hands_it(store: Path) -> None:
    """LINK 3-4, AND THE DEFECT. The real mint, given the real update-path value, mints nothing.

    Same function, same store, same user — the ONLY variable against the next test is the SHAPE
    of the argument. That is what isolates the cause to the shape rather than to the mint.
    """
    changed = _diff_jira_vs_local(
        {"assignee": dict(_RAW_DC_ASSIGNEE)},
        {"assignee": "", "title": "t", "description": "", "status": "open"},
        inbound_mapper=_DCMapper(),
    )

    air._ensure_inbound_assignee_identity(changed["assignee"], repo_root=str(store))

    assert rebar.resolve_mapping("jira", _DC_USER, repo_root=store) is None, (
        "the update path DID mint — the defect is fixed and this test should be retired"
    )
    # And nothing was minted under the displayName either, which would be the wrong key: on
    # Cloud the scalar is the displayName, so a mint keyed on it would be a second defect.
    assert rebar.resolve_mapping("jira", "Administrator", repo_root=store) is None, (
        "an identity was minted keyed on the DISPLAY NAME rather than the external id"
    )


def test_the_same_mint_works_on_the_shape_the_create_path_hands_it(store: Path) -> None:
    """THE CONTROL. One variable changed — the raw user object — and the mint fires.

    Without this the previous test would only show "no identity appeared", which is equally
    consistent with a broken store, a broken resolver, or a broken mint. It is the CONTRAST
    that makes the argument shape the cause.
    """
    air._ensure_inbound_assignee_identity(dict(_RAW_DC_ASSIGNEE), repo_root=str(store))

    minted = rebar.resolve_mapping("jira", _DC_USER, repo_root=store)
    assert minted is not None, (
        "the mint failed even on the create path's raw user object, so the defect is NOT the "
        "argument shape and this diagnosis is wrong"
    )
    assert rebar.is_placeholder(minted, repo_root=store), (
        f"the create path's mint produced a non-placeholder identity {minted!r}"
    )


def test_the_mapper_already_carries_everything_the_mint_needs(store: Path) -> None:
    """THE FIX IS AVAILABLE WITHOUT NEW EXTRACTION, which is why the proposed fix is small.

    ``_map_jira_to_local_fields`` already emits ``assignee_identity`` with ``account_id``
    holding DC's username (``_identity_of:160-166`` says so explicitly, and its precedence —
    accountId first, then name — is the one 5f48 established). The differ simply does not
    forward it. Pinning this here means a fix that plumbs it through has a stated contract to
    meet, and a fix that instead loosens the dict guard to accept a bare string is visibly the
    wrong one: on Cloud that string is the displayName, not the accountId.
    """
    from rebar_reconciler import inbound_fields

    mapped = inbound_fields._map_jira_to_local_fields({"assignee": dict(_RAW_DC_ASSIGNEE)})

    assert mapped["assignee_identity"]["account_id"] == _DC_USER, (
        f"assignee_identity.account_id is {mapped['assignee_identity']['account_id']!r}, not the "
        f"DC username — the fix cannot key the mint on it"
    )
    assert mapped["assignee"] == _DC_USER, (
        "the scalar assignee is no longer the DC username; on this instance name == the "
        "external id, which is why the bare-string shape LOOKS usable on DC and is not on Cloud"
    )

    changed = _diff_jira_vs_local(
        {"assignee": dict(_RAW_DC_ASSIGNEE)},
        {"assignee": "", "title": "t", "description": "", "status": "open"},
        inbound_mapper=_DCMapper(),
    )
    assert "assignee_identity" not in changed, (
        "the differ now forwards assignee_identity — if so the mint can key on it and this "
        "defect's mechanism has changed"
    )
