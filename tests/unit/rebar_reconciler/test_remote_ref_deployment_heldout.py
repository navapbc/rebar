"""HELD-OUT pin on `RemoteRef.instance` and the `remote_ref()` port member (ticket 6a91, epic e369).

`RemoteRef` was port vocabulary that no production code populated: in `src/` there was only the
frozen dataclass and its docstring, while the TEST DOUBLE
(`tests/unit/rebar_reconciler/backend_support.py`) had implemented a `remote_ref()` method since
J7. The contract tests therefore ran against a fake strictly MORE capable than production — the
asymmetry underneath this ticket. `Backend` now declares `remote_ref()` and both real backends
implement it.
"""

from __future__ import annotations

import pytest

from rebar_reconciler._backend import RemoteRef
from rebar_reconciler.adapters.jira_family import instance_from_base_url

# ---------------------------------------------------------------------------
# The normalisation helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://jira.example.com/jira", "https://jira.example.com/jira/"),  # trailing slash
        ("https://JIRA.example.com/jira", "https://jira.example.com/jira"),  # host case
        ("https://jira.example.com:443/jira", "https://jira.example.com/jira"),  # default port
        ("http://jira.example.com/jira", "https://jira.example.com/jira"),  # scheme
        ("jira.example.com/jira", "https://jira.example.com/jira"),  # scheme omitted
    ],
)
def test_equivalent_spellings_of_one_deployment_agree(a: str, b: str) -> None:
    """THE HALF THAT MATTERS. A helper that merely echoed its input would pass the
    "different deployments differ" test below trivially; only this half proves it NORMALISES.
    Two spellings of one instance yielding two labels would make one deployment look like two."""
    assert instance_from_base_url(a) == instance_from_base_url(b) != ""


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://one.example.com/jira", "https://two.example.com/jira"),  # host
        ("https://jira.example.com/alpha", "https://jira.example.com/beta"),  # context path
        ("https://jira.example.com:8080/jira", "https://jira.example.com/jira"),  # non-default port
    ],
)
def test_distinct_deployments_get_distinct_labels(a: str, b: str) -> None:
    """Including the two cases a naive host-only implementation would merge: a shared host that
    differs only by CONTEXT PATH (common on Data Center, which is often served at `/jira`), and a
    NON-default port, which genuinely distinguishes."""
    assert instance_from_base_url(a) != instance_from_base_url(b)


def test_an_unusable_base_url_degrades_to_empty_rather_than_raising() -> None:
    """A backend that cannot name its deployment should be UNNAMED, not unbuildable.

    This feeds an identity label, not a connection, so raising here would turn a cosmetic gap
    into a construction failure.
    """
    for junk in ("", "   ", "://"):
        assert instance_from_base_url(junk) == ""


# ---------------------------------------------------------------------------
# The port member, on the REAL backends
# ---------------------------------------------------------------------------


class _StubTransport:
    """Enough of a transport to construct a backend. Nothing here is exercised."""

    def __getattr__(self, _name: str):  # pragma: no cover - never called
        raise AssertionError("remote_ref must not touch the transport")


def _real_backends(instance: str):
    """Both CONCRETE backends, constructed directly with an explicit instance.

    Constructed WITHOUT any ambient config on purpose — that is the assertion, not a convenience.
    If `remote_ref()` ever resolves settings when called, these constructions start needing a
    config fixture and this helper breaks, which is exactly the signal wanted.
    """
    from rebar_reconciler.adapters.jira.backend import JiraBackend
    from rebar_reconciler.adapters.jira_datacenter.backend import JiraDataCenterBackend

    return [
        JiraBackend(transport=_StubTransport(), instance=instance),
        JiraDataCenterBackend(transport=_StubTransport(), instance=instance),
    ]


def test_both_real_backends_return_the_instance_they_were_constructed_with() -> None:
    """The port member is REAL in production, not only on the fake.

    Asserts the returned `instance` is EXACTLY the injected value. There is no tension between
    "no ambient config" and "non-empty instance": it is non-empty BECAUSE the test passes one in,
    and that is precisely what proves the method reads constructor state.
    """
    for backend in _real_backends("jira.example.com/jira"):
        ref = backend.remote_ref("DIG-1234")
        assert isinstance(ref, RemoteRef)
        assert ref.instance == "jira.example.com/jira", (
            f"{type(backend).__name__}.remote_ref() returned instance {ref.instance!r} rather "
            f"than the value it was constructed with — it is finding the value somewhere else"
        )
        assert ref.remote_id == "DIG-1234"
        assert ref.vendor == backend.vendor


def test_a_backend_built_without_an_instance_returns_the_empty_default() -> None:
    """Distinguishes "used what it was given" from "found one somewhere".

    Without this, a `remote_ref()` that quietly resolved config would still satisfy the test
    above whenever the ambient config happened to match.
    """
    for backend in _real_backends(""):
        assert backend.remote_ref("DIG-1").instance == "", (
            f"{type(backend).__name__}.remote_ref() produced an instance from nowhere; it must "
            f"read only what the constructor was given"
        )


def test_two_deployments_of_the_same_vendor_produce_unequal_refs() -> None:
    """The criterion the field exists for, at the level it actually operates.

    `RemoteRef` is frozen and value-equal, so this is a direct `==` comparison.
    """
    from rebar_reconciler.adapters.jira_datacenter.backend import JiraDataCenterBackend

    one = JiraDataCenterBackend(transport=_StubTransport(), instance="one.example.com/jira")
    two = JiraDataCenterBackend(transport=_StubTransport(), instance="two.example.com/jira")

    assert one.remote_ref("DIG-1") != two.remote_ref("DIG-1")
    assert one.remote_ref("DIG-1") == JiraDataCenterBackend(
        transport=_StubTransport(), instance="one.example.com/jira"
    ).remote_ref("DIG-1"), "same deployment + same id must be value-equal"


def test_vendor_already_separates_cloud_from_data_center() -> None:
    """Recorded because it changes what `instance` is FOR.

    Cloud and DC never needed `instance` to be told apart — their `vendor` strings differ. What
    `instance` disambiguates is two deployments of the SAME vendor.
    """
    cloud, dc = _real_backends("same.example.com/jira")
    assert cloud.vendor != dc.vendor
    assert cloud.remote_ref("DIG-1") != dc.remote_ref("DIG-1")


# ---------------------------------------------------------------------------
# The KNOWN LIMITATION, pinned so it cannot be silently assumed fixed
# ---------------------------------------------------------------------------


def test_instance_does_NOT_prevent_local_id_collision_between_deployments() -> None:
    """A REGRESSION GUARD ON A LIMITATION, not on a feature — read the docstring before "fixing".

    `RemoteRef`'s docstring used to claim `instance` exists "so two instances of the same vendor
    never collide". For the value itself that is true. For the LOCAL TICKET ID — the thing that
    actually collides in the store — it is FALSE, and this test pins the false half so a future
    reader cannot assume 6a91 solved it.

    `_jira_key_to_local_id` is `"jira-" + jira_key.lower()` and consults nothing else, so two DC
    deployments that each own a project `DIG` both mint `jira-dig-123` no matter what their
    `RemoteRef.instance` says. Making the id instance-aware would change the id scheme for every
    existing Jira-sourced ticket — a breaking, store-wide migration that is deliberately out of
    scope here. If that migration is ever done, THIS test is the one that should go red.
    """
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    one = _jira_key_to_local_id("DIG-123")
    two = _jira_key_to_local_id("DIG-123")

    assert one == two == "jira-dig-123", (
        "the local-id scheme changed; if it is now deployment-aware, 6a91's recorded limitation "
        "is obsolete and both ADRs plus RemoteRef's docstring must be updated to match"
    )
