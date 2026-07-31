"""HELD-OUT: inbound ghost-identity minting under the Data Center backend (story J7,
epic e369; folded-in bug 5f48).

TWO defects that COMPOUND, which is why they are pinned together in one file:

(a) ``_ensure_inbound_assignee_identity`` returns early unless the assignee object
    carries a non-empty ``accountId``. ``adapters/jira_family/identity_model.py`` is
    explicit that **DC has no accountId at all** — DC users carry ``name``/``key``
    (REST v2) — and the function is called with the RAW Jira user object. So on a DC
    deployment it exits at the guard and Cloud mints ghost identities while DC
    silently mints none. NOTHING RAISES; the mint is simply never attempted.

(b) The backend vendor is ALSO used as the creation channel
    (``ensure_identity_for(vendor, …, creation_channel=vendor)``), and
    ``"jira-datacenter"`` is not in ``CREATION_CHANNELS``. That rejection is real but
    unreachable while (a) short-circuits first — it fires the moment anyone teaches
    the guard DC's ``name``. Because the call sits inside
    ``except Exception:  # best-effort ghost mint``, logged at DEBUG, fixing (a) ALONE
    converts a silent no-op into a SWALLOWED EXCEPTION, which is strictly harder to
    diagnose. Convergence is unaffected either way — epic AC6's premise.

So ``test_fixing_only_the_guard_would_swallow_a_channel_rejection`` is the load-bearing
test here: it is what stops a future contributor from landing half the fix.

The store vocabulary must NOT grow a per-deployment channel: that would fork
``CREATION_CHANNELS``, contradict the epic's shared-identity decision, fragment one
human's identity across deployments, and drag in a migration the epic avoids.

Patching note: the reconciler resolves the backend through a FUNCTION-LOCAL import,
which binds at CALL time, so these tests patch ``select_backend`` on its DEFINING
module (``rebar_reconciler._backend_registry``), never a local alias.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest

import rebar
import rebar_reconciler._backend_registry as registry
import rebar_reconciler.apply_inbound_records as air

#: A DC assignee exactly as REST v2 returns it: a username, and NO accountId.
DC_ASSIGNEE = {"name": "dcuser", "key": "dcuser", "displayName": "DC User"}


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for a in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "d@e.com"),
        ("git", "config", "user.name", "d"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(a, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _real_backend(kind: str):
    """The REAL backend object, not a hand-rolled stub.

    A stub declaring only ``vendor`` would pass this file's assertions while the actual
    backend failed to declare its identity family — the stub-drifts-from-reality failure
    mode. Using the real class means these tests break if a backend stops declaring it.
    """
    from .backend_support import FakeTransport

    if kind == "jira":
        from rebar_reconciler.adapters.jira.backend import JiraBackend

        return JiraBackend(transport=FakeTransport())
    from rebar_reconciler.adapters.jira_datacenter.backend import JiraDataCenterBackend

    return JiraDataCenterBackend(transport=FakeTransport())


@pytest.fixture
def dc_backend_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the configured backend the real DC one, patched at its DEFINING module."""
    backend = _real_backend("jira-datacenter")
    assert backend.vendor == "jira-datacenter", "precondition: this is the DC backend"
    monkeypatch.setattr(registry, "select_backend", lambda *a, **k: backend, raising=True)


def _identity_count(store: Path) -> int:
    return len(rebar.list_tickets(ticket_type="identity", repo_root=str(store)))


# ---------------------------------------------------------------------------
# (a) the active gap — DC assignees mint nothing at all
# ---------------------------------------------------------------------------


def test_a_dc_shaped_assignee_mints_a_ghost_identity(store: Path, dc_backend_selected) -> None:
    """The headline defect. A DC assignee has a ``name`` and no ``accountId``, so today
    the guard returns early and NOTHING is minted — Cloud gets ghost identities on
    inbound and DC silently does not."""
    air._ensure_inbound_assignee_identity(DC_ASSIGNEE, repo_root=str(store))

    ident = rebar.resolve_mapping("jira", "dcuser", repo_root=str(store))
    assert ident is not None, (
        "no identity was minted for a DC assignee — the accountId guard returned early "
        "on a user object that legitimately has no accountId (DC has none at all)"
    )
    assert rebar.is_placeholder(ident, repo_root=str(store)) is True


def test_the_dc_identity_is_minted_under_the_SHARED_jira_provider(
    store: Path, dc_backend_selected
) -> None:
    """HELD-OUT edge. The provider must be the FAMILY name ``jira``, not the vendor
    string ``jira-datacenter`` — otherwise one human resolves to two different
    identities depending on which deployment they were seen from, which is exactly the
    fragmentation the epic's shared-identity decision exists to prevent."""
    air._ensure_inbound_assignee_identity(DC_ASSIGNEE, repo_root=str(store))

    assert rebar.resolve_mapping("jira", "dcuser", repo_root=str(store)) is not None
    assert rebar.resolve_mapping("jira-datacenter", "dcuser", repo_root=str(store)) is None, (
        "the identity was keyed under the per-deployment vendor string; it must use the "
        "shared 'jira' family provider"
    )


def test_the_mint_is_observed_to_SUCCEED_not_merely_to_not_raise(
    store: Path, dc_backend_selected
) -> None:
    """HELD-OUT edge, and the reason this file exists. The call site swallows EVERY
    exception at DEBUG, so "nothing raised" is true even when nothing worked. Assert
    the store actually grew."""
    before = _identity_count(store)
    air._ensure_inbound_assignee_identity(DC_ASSIGNEE, repo_root=str(store))
    assert _identity_count(store) == before + 1, (
        "identity count did not grow — the mint was swallowed, not performed"
    )


def test_dc_mint_is_idempotent(store: Path, dc_backend_selected) -> None:
    for _ in range(2):
        air._ensure_inbound_assignee_identity(DC_ASSIGNEE, repo_root=str(store))
    matches = [
        t
        for t in rebar.list_tickets(ticket_type="identity", repo_root=str(store))
        if {"provider": "jira", "external_id": "dcuser"} in t.get("mappings", [])
    ]
    assert len(matches) == 1


# ---------------------------------------------------------------------------
# (b) the landmine — why the two halves cannot be split
# ---------------------------------------------------------------------------


def test_fixing_only_the_guard_would_swallow_a_channel_rejection(
    store: Path, dc_backend_selected, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """LOAD-BEARING. Simulate the half-fix — guard accepts DC's name, channel still the
    raw vendor — and prove the resulting rejection is SWALLOWED at DEBUG rather than
    surfaced. This is why (a) and (b) must land together: the obvious one-line fix to
    (a) turns a silent no-op into a silent exception, which is harder to diagnose, and
    the pass converges either way (epic AC6).
    """
    from rebar.reducer._version import CREATION_CHANNELS, validate_creation_channel

    # The vendor string is genuinely not a member of the store vocabulary.
    assert "jira-datacenter" not in CREATION_CHANNELS
    with pytest.raises(ValueError, match="invalid creation_channel"):
        validate_creation_channel("jira-datacenter")

    # Half-fixed world: the mint is reached, but stamped with the raw vendor.
    # Capture the ORIGINAL first: calling `rebar.ensure_identity_for` from inside the
    # replacement would re-enter the patched attribute and recurse. That is not
    # hypothetical — the first version of this test did exactly that, and PASSED,
    # because RecursionError is an Exception and the call site swallows every
    # exception at DEBUG. Both assertions below were satisfied by the wrong cause.
    real_mint = rebar.ensure_identity_for

    def _raw_vendor_mint(*args: Any, **kwargs: Any) -> str:
        return real_mint(*args, **{**kwargs, "creation_channel": "jira-datacenter"})

    monkeypatch.setattr(rebar, "ensure_identity_for", _raw_vendor_mint, raising=True)

    before = _identity_count(store)
    with caplog.at_level(logging.DEBUG, logger=air.logger.name):
        air._ensure_inbound_assignee_identity(DC_ASSIGNEE, repo_root=str(store))

    assert _identity_count(store) == before, "expected the half-fixed mint to write nothing"

    swallowed = [r for r in caplog.records if "could not ensure identity" in r.message]
    assert swallowed, "the channel rejection was not even logged — it vanished entirely"

    # Pin the CAUSE, not merely that something was swallowed. Without this, any
    # exception at all (a typo, a recursion) satisfies the test — which is exactly how
    # the first draft of this test passed for the wrong reason.
    causes = [
        repr(r.exc_info[1]) if r.exc_info else ""
        for r in swallowed  # type: ignore[index]
    ]
    assert any("invalid creation_channel" in c and "jira-datacenter" in c for c in causes), (
        "the swallowed exception was NOT the channel rejection this test exists to "
        f"demonstrate; got: {causes}"
    )


def test_creation_channels_vocabulary_is_unchanged() -> None:
    """The fix must NOT widen the store vocabulary. A per-deployment channel forks it,
    contradicts the epic's shared-identity decision, and needs a migration the epic
    explicitly avoids."""
    from rebar.reducer._version import CREATION_CHANNELS

    assert CREATION_CHANNELS == frozenset({"cli", "mcp", "python", "jira", "import", "unknown"}), (
        "CREATION_CHANNELS changed — the DC fix must map to the shared 'jira', not add a channel"
    )


# ---------------------------------------------------------------------------
# Cloud regression guard — the existing behaviour must not shift
# ---------------------------------------------------------------------------


def test_cloud_accountid_path_is_unchanged(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registry, "select_backend", lambda *a, **k: _real_backend("jira"), raising=True
    )
    air._ensure_inbound_assignee_identity(
        {"accountId": "acct-cloud-9", "displayName": "Cloud User"}, repo_root=str(store)
    )
    assert rebar.resolve_mapping("jira", "acct-cloud-9", repo_root=str(store)) is not None


def test_an_assignee_with_neither_identifier_still_mints_nothing(
    store: Path, dc_backend_selected
) -> None:
    """The guard must narrow, not vanish: a user object with no accountId AND no name
    is still not mintable, and must not raise."""
    before = _identity_count(store)
    air._ensure_inbound_assignee_identity({"displayName": "No Ids"}, repo_root=str(store))
    air._ensure_inbound_assignee_identity("just a string", repo_root=str(store))
    assert _identity_count(store) == before


def test_both_jira_backends_declare_the_shared_identity_family() -> None:
    """The declaration this design rests on. If a backend stops declaring
    ``identity_family``, the core falls back to its per-deployment ``vendor`` and DC
    mints silently break again — so pin it on the REAL classes, not a stub."""
    for kind in ("jira", "jira-datacenter"):
        backend = _real_backend(kind)
        assert backend.identity_family == "jira", (
            f"{kind} backend must declare the shared 'jira' identity family; "
            f"got {backend.identity_family!r}"
        )
