"""MUTATION CHECK for J11's rewritten inbound-assignee oracle (ticket 5200).

The live cell ``test_the_inbound_assignee_mints_a_jira_family_identity`` used to compare
``rebar.ensure_identity_for(...)``'s return value against itself. ``ensure_identity_for`` is
create-or-reuse, so that oracle MINTED the thing it was checking for and could not fail. It has
been rewritten to observe the identity REGISTRY across the pass, through the read-only
``rebar.resolve_mapping``.

An oracle rewritten for discriminating power has to be shown to HAVE it, and the live cell cannot
demonstrate that — a green live run is consistent with a tautology. So the two halves are pinned
HERE, harness-free:

  * ``resolve_mapping`` returns None on a store nothing has minted into, so the rewritten
    oracle's ``minted is not None`` assertion genuinely FIRES when the pass mints nothing;
  * driving the PRODUCTION mint (``apply_inbound_records._ensure_inbound_assignee_identity``,
    the function the inbound apply actually calls) makes it resolve — so the oracle can also
    legitimately pass, and passes for the mint rather than for its own side effect.

Deliberately in ``tests/unit/``: a module under ``tests/external/live_jira_dc/`` without a
harness skipif does not run harness-free — the autouse fixture burns the budget and then errors.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar_reconciler.apply_inbound_records as air

_DC_USER = "admin"


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


def test_the_oracle_can_go_red_because_resolve_mapping_never_creates(store: Path) -> None:
    """THE HALF THE OLD ORACLE LACKED. An unminted store resolves to None, so RED is reachable.

    This is the whole point of swapping ``ensure_identity_for`` for ``resolve_mapping``: the
    latter is a pure read. If it created on miss, the live oracle would be a tautology again and
    no live run could ever detect bug 5f48's silent swallow.
    """
    assert rebar.resolve_mapping("jira", _DC_USER, repo_root=store) is None, (
        "resolve_mapping returned an id for a user nothing has minted — it is not read-only, "
        "and the rewritten live oracle is therefore still unable to fail"
    )
    # Called twice on purpose: a lazily-creating implementation could return None once and an
    # id after, which would make the live oracle pass on its own second look.
    assert rebar.resolve_mapping("jira", _DC_USER, repo_root=store) is None, (
        "the second resolve_mapping returned an id, so the first call had a creating side effect"
    )


def test_the_oracle_goes_green_on_the_real_dc_mint(store: Path) -> None:
    """THE OTHER HALF. The PRODUCTION mint satisfies every clause the live oracle asserts.

    Driving ``_ensure_inbound_assignee_identity`` — not ``ensure_identity_for`` — is deliberate:
    it is the function the inbound apply calls, it takes the RAW Jira user object, and for Data
    Center that object has NO ``accountId``, only ``name`` (bug 5f48's exact shape). A test that
    called the registry directly would prove the oracle self-consistent while saying nothing
    about whether the DC path reaches it.
    """
    air._ensure_inbound_assignee_identity(
        {"name": _DC_USER, "displayName": _DC_USER}, repo_root=str(store)
    )

    minted = rebar.resolve_mapping("jira", _DC_USER, repo_root=store)
    assert minted is not None, (
        f"the production inbound mint did not register jira/{_DC_USER!r} from a DC-shaped user "
        f"object (no accountId, only `name`) — the live oracle would be red for a real reason"
    )
    assert rebar.is_placeholder(minted, repo_root=store), (
        f"the minted identity {minted!r} is not a placeholder, so the live oracle's ghost clause "
        f"would fail even on a correct pass"
    )
    assert rebar.resolve_mapping("jira-datacenter", _DC_USER, repo_root=store) is None, (
        "the mint ALSO registered under a `jira-datacenter` provider, so the live oracle's "
        "anti-fork clause would be red — the deployment belongs in RemoteRef.instance"
    )
