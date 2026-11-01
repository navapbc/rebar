"""Pin the Data Center transport's unwrapped payload contract (J6).

Reuse the shared verified-fake shape contract for Cloud and DC so mapper inputs
cannot diverge. Inject the client for offline tests without the optional extra.
The external Jira test covers vendor behavior. Keep issue-resource method ownership.

Held-out oracles merged from test_jira_dc_transport_heldout: Pin Data Center transport security,
registration, and retry (J6).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from _jira_shape_contract import assert_comment_map_shape, assert_search_shape


class _LibIssue:
    """Stands in for ``jira.Issue`` — ATTRIBUTE access, not a mapping.

    Deliberately not dict-like: a transport that forwards the library object
    instead of unwrapping it fails the shape contract rather than passing by
    coincidence.
    """

    def __init__(self, key: str, fields: dict[str, Any]) -> None:
        self.key = key
        self.fields = type("Fields", (), dict(fields))()
        self.raw = {"key": key, "fields": dict(fields)}

    def add_field_value(self, field: str, value: str) -> None:
        """Mirror jira.resources.Issue.add_field_value(field, value).

        The method belongs to an Issue, not the JIRA client, and takes two
        arguments. A fake with the wrong surface hid a failing add_label call."""
        current = list(self.raw["fields"].get(field) or [])
        current.append(value)
        self.raw["fields"][field] = current
        setattr(self.fields, field, current)


class _LibComment:
    def __init__(self, cid: str, body: str) -> None:
        self.id = cid
        self.body = body
        self.raw = {"id": cid, "body": body}


class _LibProperty:
    def __init__(self, value: Any) -> None:
        self.value = value


class FakeJiraClient:
    """A minimal, stateful stand-in for ``jira.JIRA``."""

    def __init__(self) -> None:
        self.issues: dict[str, _LibIssue] = {
            "DC-1": _LibIssue("DC-1", {"summary": "existing", "description": "plain text"})
        }
        self._next = 2

    def create_issue(self, **fields: Any) -> _LibIssue:
        key = f"DC-{self._next}"
        self._next += 1
        self.issues[key] = _LibIssue(key, {"summary": fields.get("summary", ""), "description": ""})
        return self.issues[key]

    def issue(self, key: str, **_k: Any) -> _LibIssue:
        return self.issues[key]

    def search_issues(self, _jql: str, **_k: Any) -> list[_LibIssue]:
        issues = list(self.issues.values())
        start_at = _k.get("startAt", 0)
        max_results = _k.get("maxResults")
        if max_results is None:
            return issues[start_at:]
        return issues[start_at : start_at + max_results]

    def comments(self, key: str) -> list[_LibComment]:
        return [_LibComment("10001", "a comment")] if key in self.issues else []

    def add_comment(self, key: str, body: str) -> _LibComment:
        return _LibComment("10002", body)

    def issue_property(self, key: str, property_key: str) -> _LibProperty:
        assert key in self.issues
        assert property_key == "local_id"
        return _LibProperty({"ticket": "local-123"})


@pytest.fixture
def transport() -> Any:
    """The DC transport with a fake client injected.

    Pins the constructor seam: the transport accepts its client so it is testable
    without the extra installed and without a network.
    """
    from rebar.reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

    return JiraDataCenterTransport(client=FakeJiraClient(), project="DC")


def test_search_issues_returns_rebar_raw_shape(transport: Any) -> None:
    result = transport.search_issues("project = DC")

    assert_search_shape(result)
    assert all(isinstance(i, dict) for i in result), (
        "search_issues must yield plain dicts, not library objects"
    )


def test_get_issue_returns_a_plain_dict(transport: Any) -> None:
    issue = transport.get_issue("DC-1")

    assert isinstance(issue, dict), f"get_issue must return a dict, got {type(issue)}"
    assert isinstance(issue.get("key"), str) and issue["key"]
    assert isinstance(issue.get("fields"), dict)


def test_create_issue_returns_a_plain_dict_with_the_new_key(transport: Any) -> None:
    created = transport.create_issue({"summary": "new one", "issuetype": "Task"})

    assert isinstance(created, dict)
    assert isinstance(created.get("key"), str) and created["key"]
    # the created issue is really there — a create that returned a shape but
    # persisted nothing would pass a shape-only assertion
    assert transport.get_issue(created["key"])["key"] == created["key"]


def test_comment_map_matches_the_shared_contract(transport: Any) -> None:
    assert_comment_map_shape(transport.get_comment_map("DC"))


def test_description_is_plain_text_not_an_adf_document(transport: Any) -> None:
    """DC speaks REST v2, where descriptions are strings. A dict would mean we are
    on Cloud's v3 + ADF path — the exact difference this epic exists to model."""
    description = transport.get_issue("DC-1")["fields"].get("description")

    assert description is None or isinstance(description, str), (
        f"DC descriptions must be plain text, got {type(description)}"
    )


def test_missing_extra_raises_naming_the_install_command(monkeypatch) -> None:
    """A missing ``[jira-datacenter]`` extra must name the exact install command.

    This is the "clear install-naming error" the story promised, mirroring
    ``rebar.llm.runner._import_pydantic_ai``. It had no test: the message existed
    in ``transport.py`` but nothing pinned it, so a reword could silently leave an
    operator with an ImportError that does not say what to install. A completion
    verification run on ticket 9fd4 flagged exactly that gap.

    ``monkeypatch.setitem`` on ``sys.modules`` is used rather than
    ``setdefault``: binding the key to ``None`` makes ``import jira`` raise
    ImportError, and monkeypatch restores the previous value (including absence)
    at teardown, so this cannot leak into a sibling test — the failure mode
    recorded on ticket 2bc7.
    """
    import sys

    from rebar.reconciler.adapters.jira_datacenter import transport as _t

    monkeypatch.setitem(sys.modules, "jira", None)

    with pytest.raises(ImportError) as caught:
        _t._jira_client_class()

    message = str(caught.value)
    assert "pip install 'nava-rebar[jira-datacenter]'" in message, (
        "the error must name the exact install command an operator can copy; got: " + message
    )


def test_add_label_appends_without_clobbering_existing_labels(transport) -> None:
    """``add_label`` must APPEND, and must go through the Issue resource.

    Regression for a bug the live tier caught on its first real execution: the
    transport called ``self._client.add_field_value(...)``, but ``jira.JIRA``
    has no such attribute — only ``jira.resources.Issue`` does, with a
    two-argument signature. The method could therefore never have worked. This
    pins both halves: the call reaches the issue resource, and the semantics are
    append (a read-modify-write of the whole list would clobber a concurrent
    edit).
    """
    issue = transport._client.issues["DC-1"]
    issue.raw["fields"]["labels"] = ["pre-existing"]

    transport.add_label("DC-1", "rebar-added")

    labels = transport.get_issue("DC-1")["fields"]["labels"]
    assert labels == ["pre-existing", "rebar-added"], (
        f"add_label must append without resetting existing labels; got {labels!r}"
    )


def test_get_issue_property_unwraps_the_pycontribs_value(transport) -> None:
    """The access-check core consumes the stored JSON value, not a resource wrapper."""
    assert transport.get_issue_property("DC-1", "local_id") == {"ticket": "local-123"}


# ── held-out oracles (merged from test_jira_dc_transport_heldout) ──


def _built_client_kwargs(monkeypatch: Any, **overrides: Any) -> dict[str, Any]:
    """Construct the client for real, capturing the kwargs it was built with.

    Drives the production seam ``build_client_from_settings(settings)`` with a
    real ``JiraDataCenterSettings``, swapping only the library class — so the
    assertions below see exactly what a live run would pass to ``jira.JIRA``.
    """
    from rebar.reconciler.adapters.jira_datacenter import transport as _t
    from rebar.reconciler.adapters.jira_datacenter.settings import JiraDataCenterSettings

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
    from rebar.reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

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


def test_the_dc_backend_is_registered_and_selectable() -> None:
    """``select_backend`` resolves 'jira-datacenter' after importing adapters.

    Without the self-registration import in ``adapters/__init__.py`` this raises
    ``BackendRegistryError`` at runtime with every other file correct — and no
    transport unit test would notice.
    """
    import rebar.reconciler.adapters  # noqa: F401  (side effect: registers factories)
    from rebar.reconciler._backend_registry import _REGISTRY

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
    from rebar.reconciler._backend_registry import (
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


def test_an_http_error_is_attempted_exactly_once(monkeypatch: Any) -> None:
    """Retrying a 4xx/5xx mutation risks a DUPLICATE issue/comment. Not retried."""
    from rebar.reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

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
    from rebar.reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

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


def test_the_dc_package_does_not_import_the_cloud_acli_transport() -> None:
    """DC must not reach into the ACLI path — that is the Cloud-validated
    transport this epic must not disturb."""
    import ast
    from pathlib import Path

    from _tree_scan import parsed_python_files

    root = Path(__file__).resolve().parents[3] / "src/rebar/reconciler/adapters/jira_datacenter"
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
