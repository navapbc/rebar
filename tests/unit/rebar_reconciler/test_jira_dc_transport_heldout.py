"""HELD-OUT oracle for the Data Center transport (story J6, epic e369).

This file is HELD OUT from the implementation subagent. It pins the properties a
shape-and-happy-path spec cannot: the security posture, the retry semantics, and
the registration wiring — each of which fails silently or at an operator's first
run rather than in an obvious way.

Three are worth stating explicitly, because each has a specific way of being
"tested" without being tested at all:

* **TLS.** ``pycontribs/jira`` reads TLS config from its OPTIONS DICT — ``verify``
  is a key of ``JIRA.DEFAULT_OPTIONS`` (default ``True``), consumed as
  ``self._options["verify"]``. So an assertion phrased as "no ``verify=False``
  kwarg" is VACUOUS: no such kwarg exists, and it would pass unchanged with
  verification disabled through the options dict.
* **Registration.** A missing self-registration import makes
  ``select_backend("jira-datacenter")`` fail at RUNTIME with
  ``BackendRegistryError`` (not ``KeyError``) while every file is individually
  correct and every transport unit test passes.
* **Retry.** "It retries" is easy to assert and easy to get backwards: an HTTP
  error retried is a duplicate mutation, and a timeout NOT retried is a flaky
  reconcile. The direction is the contract, so both halves are pinned.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# TLS posture — asserted on the OPTIONS DICT the library actually reads
# ---------------------------------------------------------------------------


def _built_client_kwargs(monkeypatch: Any, **overrides: Any) -> dict[str, Any]:
    """Construct the client for real, capturing the kwargs it was built with.

    Drives the production seam ``build_client_from_settings(settings)`` with a
    real ``JiraDataCenterSettings``, swapping only the library class — so the
    assertions below see exactly what a live run would pass to ``jira.JIRA``.
    """
    from rebar_reconciler.adapters.jira_datacenter import transport as _t
    from rebar_reconciler.adapters.jira_datacenter.settings import JiraDataCenterSettings

    captured: dict[str, Any] = {}

    class _Spy:
        def __init__(self, *a: Any, **k: Any) -> None:
            captured.update(k)
            captured["_args"] = a

    settings = JiraDataCenterSettings(
        url=overrides.get("url", "https://jira.example.invalid"),
        project=overrides.get("project", "DC"),
        allow_insecure=overrides.get("allow_insecure", False),
        ca_bundle=overrides.get("ca_bundle", ""),
        pat=overrides.get("token", "pat-xyz"),
    )
    monkeypatch.setattr(_t, "_jira_client_class", lambda: _Spy, raising=False)
    _t.build_client_from_settings(settings)
    return captured


def test_tls_verification_is_never_disabled(monkeypatch: Any) -> None:
    """The effective ``options["verify"]`` must never be False.

    Checked against the options dict, not a phantom kwarg — see the module
    docstring for why the obvious phrasing cannot fail.
    """
    kwargs = _built_client_kwargs(monkeypatch, url="https://jira.example.invalid", token="pat-xyz")
    options = kwargs.get("options") or {}

    assert options.get("verify", True) is not False, (
        f"TLS verification must never be disabled; got options={options!r}. "
        f"An internal CA is configured via a CA-bundle path, never by turning verify off."
    )
    assert kwargs.get("verify") is not False, "and not via a bare kwarg either"


def test_a_ca_bundle_configures_verification_rather_than_disabling_it(monkeypatch: Any) -> None:
    """The sanctioned way to trust an internal CA: a bundle PATH in ``verify``."""
    kwargs = _built_client_kwargs(
        monkeypatch,
        url="https://jira.example.invalid",
        token="pat-xyz",
        ca_bundle="/etc/ssl/corp-ca.pem",
    )
    options = kwargs.get("options") or {}

    assert options.get("verify") == "/etc/ssl/corp-ca.pem", (
        f"a ca_bundle must land in options['verify'] as a path; got {options!r}"
    )


def test_the_pat_is_sent_as_bearer_token_not_basic_auth(monkeypatch: Any) -> None:
    kwargs = _built_client_kwargs(monkeypatch, url="https://jira.example.invalid", token="pat-xyz")

    assert kwargs.get("token_auth") == "pat-xyz", (
        f"DC authenticates with a PAT bearer token; got {sorted(kwargs)}"
    )
    assert "basic_auth" not in kwargs, "DC must not fall back to basic auth"


_PAT = "pat-xyz"


class _CredentialCarryingClient:
    """A client that genuinely HOLDS the PAT and prints it in its own repr.

    Auth/session objects routinely repr their own attributes, so a transport that
    folds its client into its repr discloses the credential. Passing ``object()``
    here — as this test's earlier form did — puts no credential within reach at
    all, which is precisely why the assertion below could not fail.
    """

    def __init__(self, token: str) -> None:
        self.token_auth = token

    def __repr__(self) -> str:
        return f"<Client token_auth={self.token_auth!r}>"


def test_the_pat_never_appears_in_the_repr_of_the_transport() -> None:
    """A credential that leaks into logs via repr is a real disclosure path."""
    from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

    client = _CredentialCarryingClient(_PAT)
    assert _PAT in repr(client), (
        "fixture self-check: the client must actually carry the credential, or this "
        "test is asserting the absence of something that was never present"
    )

    t = JiraDataCenterTransport(client=client, project="DC")

    for rendering, how in ((repr(t), "repr()"), (f"{t}", "f-string/str()")):
        assert _PAT not in rendering, (
            f"the PAT reached a log-facing rendering of the transport via {how}: "
            f"{rendering!r}. A transport must not surface its client's credential — "
            f"not directly, and not by delegating to the client's own repr."
        )


# ---------------------------------------------------------------------------
# Registration — the failure that only appears at an operator's first run
# ---------------------------------------------------------------------------


def test_the_dc_backend_is_registered_and_selectable() -> None:
    """``select_backend`` resolves 'jira-datacenter' after importing adapters.

    Without the self-registration import in ``adapters/__init__.py`` this raises
    ``BackendRegistryError`` at runtime with every other file correct — and no
    transport unit test would notice.
    """
    import rebar_reconciler.adapters  # noqa: F401  (side effect: registers factories)
    from rebar_reconciler._backend_registry import _REGISTRY

    assert "jira-datacenter" in _REGISTRY, (
        f"the DC backend did not self-register; registered keys: {sorted(_REGISTRY)}. "
        f"adapters/__init__.py must import its backend module for the register() side effect."
    )


def _config_naming_backend(key: str) -> Any:
    """The minimal duck-typed config ``select_backend`` reads: ``.reconciler.backend``."""
    return SimpleNamespace(reconciler=SimpleNamespace(backend=key))


def test_an_unknown_backend_raises_the_registry_error_not_key_error() -> None:
    """Pins the exception the negative path actually RAISES, not just a class
    relationship: the earlier form never called ``select_backend`` at all.

    ``_backend_registry`` raises ``BackendRegistryError``; a guard written as
    ``pytest.raises(KeyError)`` would never match and would protect nothing.
    """
    from rebar_reconciler._backend_registry import (
        BackendRegistryError,
        _reset_registry_for_test,
        register,
        select_backend,
    )

    sentinel = object()

    with _reset_registry_for_test():
        register("known-backend-for-this-test")(lambda _config: sentinel)

        # Positive control: the lookup path resolves, so the miss below is a real
        # miss and not a selector that raises on everything.
        assert select_backend(_config_naming_backend("known-backend-for-this-test")) is sentinel

        with pytest.raises(BackendRegistryError) as excinfo:
            select_backend(_config_naming_backend("definitely-not-a-registered-backend"))

    assert not isinstance(excinfo.value, KeyError), (
        "the miss must not surface as a KeyError: if these types ever converged, "
        "tests written against either would silently both pass"
    )
    assert "definitely-not-a-registered-backend" in str(excinfo.value), (
        f"the error must name the key that missed; got {excinfo.value!s}"
    )
    assert "known-backend-for-this-test" in str(excinfo.value), (
        f"the error must enumerate the registered keys so an operator can see the "
        f"correct spelling; got {excinfo.value!s}"
    )


class FakeHttpError(Exception):
    """Shaped like ``jira.exceptions.JIRAError``: carries a ``status_code``.

    A named type rather than a bare ``Exception`` so the assertion below pins
    that THIS error propagated — a blind ``pytest.raises(Exception)`` would also
    pass on an unrelated crash (and ruff B017 flags it for exactly that reason).
    """

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status_code = status


# ---------------------------------------------------------------------------
# Retry direction — both halves, because getting it backwards is silent
# ---------------------------------------------------------------------------


def test_an_http_error_is_attempted_exactly_once(monkeypatch: Any) -> None:
    """Retrying a 4xx/5xx mutation risks a DUPLICATE issue/comment. Not retried."""
    from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

    calls = {"n": 0}

    class _HttpFailingClient:
        def issue(self, _key: str, **_k: Any) -> Any:
            calls["n"] += 1
            raise FakeHttpError(500)

    with pytest.raises(FakeHttpError):
        JiraDataCenterTransport(client=_HttpFailingClient(), project="DC").get_issue("DC-1")

    assert calls["n"] == 1, (
        f"an HTTP error must NOT be retried (duplicate-mutation risk); got {calls['n']} attempts"
    )


def test_a_connection_timeout_is_retried(monkeypatch: Any) -> None:
    """The mirror image: a transient connectivity fault IS retried, or every
    reconcile pass becomes flaky on an ordinary network blip."""
    from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

    calls = {"n": 0}

    class _FlakyThenOkClient:
        def issue(self, key: str, **_k: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                # builtin TimeoutError — since Python 3.10 this is what a raw
                # ssl/socket read-timeout surfaces as, and acli_rest retries it
                # explicitly ("read-timeout from ssl/socket layer"). The DC
                # transport claims to mirror that policy.
                raise TimeoutError("connection timed out")
            return type(
                "I",
                (),
                {"key": key, "fields": type("F", (), {})(), "raw": {"key": key, "fields": {}}},
            )()

    monkeypatch.setattr("time.sleep", lambda *_a: None)  # no real backoff wait
    JiraDataCenterTransport(client=_FlakyThenOkClient(), project="DC").get_issue("DC-1")

    assert calls["n"] > 1, "a connection timeout must be retried, not surfaced immediately"


# ---------------------------------------------------------------------------
# Collateral invariant: the live-validated Cloud transport is untouched
# ---------------------------------------------------------------------------


def test_the_dc_package_does_not_import_the_cloud_acli_transport() -> None:
    """DC must not reach into the ACLI path — that is the Cloud-validated
    transport this epic must not disturb."""
    import ast
    from pathlib import Path

    from _tree_scan import parsed_python_files

    root = (
        Path(__file__).resolve().parents[3]
        / "src/rebar/_engine/rebar_reconciler/adapters/jira_datacenter"
    )
    assert root.is_dir(), "the jira_datacenter package does not exist"

    for module in parsed_python_files(root):
        mod_name = module.path.name
        for node in ast.walk(module.tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert "acli" not in name.split(".")[-1], (
                    f"{mod_name} imports {name} — DC must not depend on the Cloud ACLI transport"
                )
