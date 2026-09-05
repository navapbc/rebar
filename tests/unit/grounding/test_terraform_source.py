"""Contract oracle for Terraform module/provider SOURCE probing (REB-640, 1c52).

``TerraformSession.probe_source(source, from_module)`` disproves an asserted
UNAVAILABILITY of a declared module/provider source when it is (a) a repo-contained
local module or (b) a registry address whose metadata is positively reachable over a
bounded HTTPS probe. It is POSITIVE-ONLY: reachable evidence ``refuted``s the claim;
an access failure ``abstain``s with a closed reason and NEVER asserts nonexistence.
It never downloads module content, never follows a cross-host redirect, and never
leaks a credential/token/path/body into evidence, receipts, or logs.

These tests assert only the OBSERVABLE contract — evidence dict, receipt dict,
requested URL/headers, finalized usage — never private structure. The registry
network is faked at the single ``terraform_source._https_probe`` seam (no live net).
"""

from __future__ import annotations

import json
import ssl
from pathlib import Path
from socket import gaierror
from types import SimpleNamespace

import pytest

from rebar import schemas
from rebar.grounding import evidence as ev

# probe_source's registry/local logic is pure-Python + stdlib; but a real local-module
# refutation parses .tf, which needs the optional extra. Import the module under test
# directly so its ABSENCE is a hard RED, never a silent skip.
pytest.importorskip("hcl2")
from rebar.grounding import terraform_source as ts
from rebar.grounding import terraform_tools as tft

DEFAULT = ts.DEFAULT_REGISTRY


# ── fixtures ─────────────────────────────────────────────────────────────────


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A root module ``infra/`` that calls a repo-contained local child ``modules/vpc``."""
    _write(
        tmp_path,
        "infra/main.tf",
        'module "vpc" {\n  source = "../modules/vpc"\n}\n'
        'module "reg" {\n  source = "terraform-aws-modules/vpc/aws"\n}\n',
    )
    _write(tmp_path, "modules/vpc/main.tf", 'output "vpc_id" {\n  value = "vpc-abc"\n}\n')
    return tmp_path


def _session(repo: Path, selected):
    return tft.open_session(repo_root=str(repo), selected=list(selected))


class _FakeNet:
    """Records every ``_https_probe`` call and returns/raises a programmed response.

    ``responder(url, headers) -> HttpResult`` (or raises). Records url + headers so a
    test can prove which endpoint was hit and whether a token was ever attached.
    """

    def __init__(self, responder):
        self.calls: list[SimpleNamespace] = []
        self._responder = responder

    def __call__(self, url, *, headers, timeout, max_bytes):
        self.calls.append(SimpleNamespace(url=url, headers=dict(headers), max_bytes=max_bytes))
        return self._responder(url, dict(headers))

    @property
    def urls(self) -> list[str]:
        return [c.url for c in self.calls]


def _ok_provider(url, headers):
    return ts.HttpResult(status=200, final_url=url, body=b'{"id":"hashicorp/aws","versions":[]}')


def _ok_module(url, headers):
    return ts.HttpResult(status=200, final_url=url, body=b'{"modules":[{"versions":[]}]}')


def _patch_net(monkeypatch, responder) -> _FakeNet:
    fake = _FakeNet(responder)
    monkeypatch.setattr(ts, "_https_probe", fake)
    return fake


def _probe(session, source, from_module="infra"):
    try:
        return session.probe_source(source, from_module=from_module)
    finally:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# HAPPY PATH (given to the implementer)
# ══════════════════════════════════════════════════════════════════════════════


def test_local_source_refutes_when_repo_contained_module(repo: Path) -> None:
    """A literal local ``source`` resolving to a repo-contained indexable module DISPROVES
    an asserted absence: ``refuted`` at tier T1, receipt ``source_kind='local_module'``."""
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.probe_source("../modules/vpc", from_module="infra")
    finally:
        session.finalize()

    e, r = res.evidence, res.receipt
    assert e["outcome"] == ev.OUTCOME_REFUTED
    assert e["reason"] is None
    assert e["job"] == ev.JOB_REFUTE
    assert e["provenance_tier"] == ev.TIER_T1
    assert e["location"]["file"].startswith("modules/vpc/")
    assert r["operation"] == "probe_source"
    assert r["outcome"] == "refuted"
    assert r["source_kind"] == "local_module"
    schemas.validator(schemas.GROUNDING).validate(e)
    schemas.validator(schemas.TERRAFORM_GROUNDING_RECEIPT).validate(r)


def test_registry_provider_refutes_on_schema_valid_metadata(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 2-part provider address whose versions metadata is a schema-valid 2xx DISPROVES
    absence: ``refuted`` at T0, ``source_kind='registry_provider'``; no download URL hit."""
    net = _patch_net(monkeypatch, _ok_provider)
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.probe_source("hashicorp/aws", from_module="infra")
    finally:
        session.finalize()

    e, r = res.evidence, res.receipt
    assert e["outcome"] == ev.OUTCOME_REFUTED
    assert e["provenance_tier"] == ev.TIER_T0
    assert r["source_kind"] == "registry_provider"
    # the default registry, provider versions endpoint, over HTTPS — never a download URL
    assert net.urls, "a registry probe must issue exactly one bounded HTTPS request"
    assert all(u.startswith(f"https://{DEFAULT}/") for u in net.urls)
    assert all("/download" not in u and "/archive" not in u for u in net.urls)
    assert any("hashicorp/aws" in u and "versions" in u for u in net.urls)
    schemas.validator(schemas.TERRAFORM_GROUNDING_RECEIPT).validate(r)


def test_registry_module_refutes_on_schema_valid_metadata(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 3-part module address (ns/name/system) that is reachable DISPROVES absence:
    ``refuted`` at T0, ``source_kind='registry_module'``."""
    net = _patch_net(monkeypatch, _ok_module)
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.probe_source("terraform-aws-modules/vpc/aws", from_module="infra")
    finally:
        session.finalize()

    assert res.evidence["outcome"] == ev.OUTCOME_REFUTED
    assert res.receipt["source_kind"] == "registry_module"
    assert any("/v1/modules/" in u for u in net.urls)
    assert all("/download" not in u for u in net.urls)


# ══════════════════════════════════════════════════════════════════════════════
# HELD OUT (withheld from the implementer)
# ══════════════════════════════════════════════════════════════════════════════

# ── AC1: local-source containment matrix ──


@pytest.mark.parametrize(
    "source",
    [
        "/etc/passwd",  # absolute
        "../../outside",  # escapes the repo
        "./missing",  # missing target
        "terraform-aws-modules/vpc/aws/../../../etc",  # not a local form / traversal
    ],
)
def test_local_source_abstains_never_asserts_absence(
    repo: Path, source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_net(monkeypatch, lambda u, h: pytest.fail(f"no network for local form: {u}"))
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.probe_source(source, from_module="infra")
    finally:
        session.finalize()
    assert res.evidence["outcome"] == ev.OUTCOME_ABSTAIN
    assert res.receipt["operation"] == "probe_source"
    assert res.receipt.get("source_kind") is None
    schemas.validator(schemas.TERRAFORM_GROUNDING_RECEIPT).validate(res.receipt)


def test_local_source_escaping_symlink_abstains(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "main.tf").write_text('output "x" {\n  value = 1\n}\n', encoding="utf-8")
    _write(tmp_path, "repo/infra/main.tf", 'module "e" {\n  source = "../linked"\n}\n')
    (tmp_path / "repo" / "linked").symlink_to(outside, target_is_directory=True)
    session = tft.open_session(repo_root=str(tmp_path / "repo"), selected=["infra/main.tf"])
    try:
        res = session.probe_source("../linked", from_module="infra")
    finally:
        session.finalize()
    assert res.evidence["outcome"] == ev.OUTCOME_ABSTAIN


def test_local_source_generated_cache_is_excluded(tmp_path: Path) -> None:
    _write(tmp_path, "infra/main.tf", 'module "v" {\n  source = "./child"\n}\n')
    _write(tmp_path, "infra/.terraform/modules/v/main.tf", 'output "y" {\n  value = 2\n}\n')
    session = tft.open_session(repo_root=str(tmp_path), selected=["infra/main.tf"])
    try:
        res = session.probe_source("./.terraform/modules/v", from_module="infra")
    finally:
        session.finalize()
    assert res.evidence["outcome"] == ev.OUTCOME_ABSTAIN


# ── AC5: registry access-error matrix maps to CLOSED abstentions, never nonexistence ──


@pytest.mark.parametrize(
    ("status", "reason_detail", "reason"),
    [
        (401, "registry_unauthorized", "private_or_internal_suspected"),
        (403, "registry_unauthorized", "private_or_internal_suspected"),
        (404, "registry_not_found", "private_or_internal_suspected"),
        (429, "registry_rate_limited", "rate_limited"),
        (500, "registry_server_error", "network_error"),
        (503, "registry_server_error", "network_error"),
    ],
)
def test_registry_status_maps_to_closed_abstention(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    reason_detail: str,
    reason: str,
) -> None:
    _patch_net(monkeypatch, lambda u, h: ts.HttpResult(status=status, final_url=u, body=b"{}"))
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.probe_source("hashicorp/aws", from_module="infra")
    finally:
        session.finalize()
    assert res.evidence["outcome"] == ev.OUTCOME_ABSTAIN
    assert res.evidence["reason"] == reason
    assert res.receipt["reason_detail"] == reason_detail
    assert res.receipt.get("source_kind") is None
    schemas.validator(schemas.TERRAFORM_GROUNDING_RECEIPT).validate(res.receipt)


@pytest.mark.parametrize(
    ("exc", "reason_detail", "reason"),
    [
        (gaierror("name resolution"), "dns_error", "network_error"),
        (ssl.SSLError("handshake"), "tls_error", "network_error"),
        (TimeoutError("read timeout"), "probe_timeout", "timeout"),
    ],
)
def test_registry_transport_error_maps_to_closed_abstention(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    reason_detail: str,
    reason: str,
) -> None:
    def boom(u, h):
        raise exc

    _patch_net(monkeypatch, boom)
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.probe_source("hashicorp/aws", from_module="infra")
    finally:
        session.finalize()
    assert res.evidence["outcome"] == ev.OUTCOME_ABSTAIN
    assert res.evidence["reason"] == reason
    assert res.receipt["reason_detail"] == reason_detail


def test_registry_malformed_metadata_abstains_parse_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_net(
        monkeypatch, lambda u, h: ts.HttpResult(status=200, final_url=u, body=b"not json <<<")
    )
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.probe_source("hashicorp/aws", from_module="infra")
    finally:
        session.finalize()
    assert res.evidence["outcome"] == ev.OUTCOME_ABSTAIN
    assert res.receipt["reason_detail"] == "malformed_metadata"
    assert res.evidence["reason"] == "parse_error"


def test_registry_oversized_metadata_abstains_parse_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def oversized(u, h):
        raise ts.OversizedMetadata("body exceeded cap")

    _patch_net(monkeypatch, oversized)
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.probe_source("hashicorp/aws", from_module="infra")
    finally:
        session.finalize()
    assert res.receipt["reason_detail"] == "oversized_metadata"
    assert res.evidence["reason"] == "parse_error"
    # the byte cap the helper is asked to enforce is the bounded 1 MiB
    # (a request WAS issued with a max_bytes bound)
    # oversized is raised by the helper, so no call recorded; assert the constant instead
    assert ts.MAX_METADATA_BYTES == 1 << 20


# ── AC6: network-target / SSRF policy — reject BEFORE any token is sent ──


@pytest.mark.parametrize(
    "source",
    [
        "http://registry.example.com/ns/name/aws",  # non-HTTPS scheme embedded
        "https://user:pw@registry.example.com/ns/name/aws",  # embedded URL credentials
        "10.0.0.5/ns/name/aws",  # literal IP host
        "internal.corp/ns/name/aws",  # private/uncredentialed custom host
    ],
)
def test_registry_target_policy_rejects_before_probe(
    repo: Path, source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_net(monkeypatch, lambda u, h: pytest.fail(f"probe must not fire for {source!r}: {u}"))
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.probe_source(source, from_module="infra")
    finally:
        session.finalize()
    assert res.evidence["outcome"] == ev.OUTCOME_ABSTAIN
    assert res.receipt["reason_detail"] in {"rejected_target", "non_registry_remote"}
    assert res.receipt.get("source_kind") is None


def test_cross_host_redirect_is_network_error_before_credential(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 3xx to a different host is rejected (no-redirect opener) as ``rejected_redirect``;
    the token is NEVER carried to the redirect target (the helper does not follow it)."""

    def redirect(u, h):
        return ts.HttpResult(status=302, final_url="https://evil.example.net/steal", body=b"")

    net = _patch_net(monkeypatch, redirect)
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.probe_source("hashicorp/aws", from_module="infra")
    finally:
        session.finalize()
    assert res.receipt["reason_detail"] == "rejected_redirect"
    assert res.evidence["reason"] == "network_error"
    # only one request was ever made, to the ORIGINAL default-registry host
    assert all(u.startswith(f"https://{DEFAULT}/") for u in net.urls)


@pytest.mark.parametrize("source", ["git::https://ex.com/mod.git", "s3::https://b/k", "./a/${x}"])
def test_remote_and_dynamic_sources_abstain_never_probe(
    repo: Path, source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_net(monkeypatch, lambda u, h: pytest.fail(f"must not probe {source!r}"))
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.probe_source(source, from_module="infra")
    finally:
        session.finalize()
    assert res.evidence["outcome"] == ev.OUTCOME_ABSTAIN
    assert res.receipt["reason_detail"] in {
        "non_registry_remote",
        "dynamic_source",
        "rejected_target",
    }


# ── AC4/AC6: ambient credentials — precedence, hostname encoding, redaction ──


_SECRET = "s3cr3t-token-value-DO-NOT-LEAK"


def _capture_auth(seen):
    def responder(u, h):
        seen.append(h.get("Authorization", ""))
        return ts.HttpResult(
            status=200, final_url=u, body=b'{"id":"x","versions":[],"modules":[{"versions":[]}]}'
        )

    return responder


def test_env_token_attached_for_credentialed_custom_host(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TF_TOKEN_registry_example_com", _SECRET)
    seen: list[str] = []
    net = _patch_net(monkeypatch, _capture_auth(seen))
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.probe_source("registry.example.com/ns/name/aws", from_module="infra")
    finally:
        session.finalize()
    assert res.evidence["outcome"] == ev.OUTCOME_REFUTED
    assert seen and seen[0] == f"Bearer {_SECRET}"
    assert all(u.startswith("https://registry.example.com/") for u in net.urls)
    # auth_source is surfaced (closed vocabulary), the token itself never is
    blob = json.dumps(res.evidence) + json.dumps(res.receipt)
    assert "auth_source=environment" in json.dumps(res.evidence)
    assert _SECRET not in blob


def test_env_token_dashed_hostname_encoding(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # a dash in the hostname encodes as a DOUBLE underscore (Terraform's rule)
    monkeypatch.setenv("TF_TOKEN_my__registry_example_com", _SECRET)
    seen: list[str] = []
    _patch_net(monkeypatch, _capture_auth(seen))
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.probe_source("my-registry.example.com/ns/name/aws", from_module="infra")
    finally:
        session.finalize()
    assert res.evidence["outcome"] == ev.OUTCOME_REFUTED
    assert seen and seen[0] == f"Bearer {_SECRET}"


def test_env_credential_beats_static_file(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "creds.tfrc.json"
    cfg.write_text(
        json.dumps({"credentials": {"registry.example.com": {"token": "FILE-TOKEN"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("TF_CLI_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("TF_TOKEN_registry_example_com", _SECRET)
    seen: list[str] = []
    _patch_net(monkeypatch, _capture_auth(seen))
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.probe_source("registry.example.com/ns/name/aws", from_module="infra")
    finally:
        session.finalize()
    assert seen and seen[0] == f"Bearer {_SECRET}"  # env wins
    assert "auth_source=environment" in json.dumps(res.evidence)
    assert "FILE-TOKEN" not in json.dumps(res.receipt)


def test_static_config_file_token_used_when_no_env(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cli.tfrc"
    cfg.write_text(
        f'credentials "registry.example.com" {{\n  token = "{_SECRET}"\n}}\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("TF_TOKEN_registry_example_com", raising=False)
    monkeypatch.setenv("TF_CLI_CONFIG_FILE", str(cfg))
    seen: list[str] = []
    _patch_net(monkeypatch, _capture_auth(seen))
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.probe_source("registry.example.com/ns/name/aws", from_module="infra")
    finally:
        session.finalize()
    assert seen and seen[0] == f"Bearer {_SECRET}"
    assert "auth_source=static-file" in json.dumps(res.evidence)
    assert _SECRET not in json.dumps(res.evidence) + json.dumps(res.receipt)


def test_default_credentials_tfrc_json_used_when_no_env_or_cli(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TF_CLI_CONFIG_FILE", raising=False)
    monkeypatch.delenv("TF_TOKEN_registry_example_com", raising=False)
    creds = tmp_path / ".terraform.d" / "credentials.tfrc.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(
        json.dumps({"credentials": {"registry.example.com": {"token": _SECRET}}}),
        encoding="utf-8",
    )
    seen: list[str] = []
    _patch_net(monkeypatch, _capture_auth(seen))
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.probe_source("registry.example.com/ns/name/aws", from_module="infra")
    finally:
        session.finalize()
    assert seen and seen[0] == f"Bearer {_SECRET}"
    assert "auth_source=static-file" in json.dumps(res.evidence)


def test_default_public_registry_needs_no_credential(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    _patch_net(monkeypatch, _capture_auth(seen))
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.probe_source("hashicorp/aws", from_module="infra")
    finally:
        session.finalize()
    assert res.evidence["outcome"] == ev.OUTCOME_REFUTED
    assert seen == [""]  # no Authorization header sent to the public default registry
    assert "auth_source=none" in json.dumps(res.evidence)


def test_credentials_helper_block_is_ignored(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "cli.tfrc"
    cfg.write_text('credential_helper "store" {\n  args = []\n}\n', encoding="utf-8")
    monkeypatch.setenv("TF_CLI_CONFIG_FILE", str(cfg))
    monkeypatch.delenv("TF_TOKEN_registry_example_com", raising=False)
    seen: list[str] = []
    _patch_net(monkeypatch, _capture_auth(seen))
    session = _session(repo, ["infra/main.tf"])
    try:
        # uncredentialed private host -> rejected before any probe
        res = session.probe_source("internal.corp/ns/name/aws", from_module="infra")
    finally:
        session.finalize()
    assert res.evidence["outcome"] == ev.OUTCOME_ABSTAIN
    assert seen == []  # helper never runs, no probe fired


# ── AC3: closed receipt-schema extension is used, never bypassed ──


def test_schema_rejects_unknown_operation_and_reason_detail() -> None:
    validator = schemas.validator(schemas.TERRAFORM_GROUNDING_RECEIPT)
    base = {
        "schema_version": 1,
        "operation": "probe_source",
        "query": {"operation": "probe_source", "source": "hashicorp/aws", "from_module": "infra"},
        "snapshot_digest": "sha256:" + "0" * 64,
        "module_digest": "sha256:" + "0" * 64,
        "backend": {
            "parser": "python-hcl2",
            "parser_version": "8.1.3",
            "analyzer": "rebar-terraform-structural",
            "analyzer_version": 1,
            "config_digest": "sha256:" + "0" * 64,
        },
        "limits": {"modules": 64, "files": 5000, "bytes": 33554432, "timeout_ms": 60000},
        "outcome": "refuted",
        "reason": None,
        "reason_detail": None,
        "result_digest": "sha256:" + "0" * 64,
        "source_kind": "registry_provider",
    }
    validator.validate(base)  # probe_source + source_kind are accepted
    from jsonschema import ValidationError

    with pytest.raises(ValidationError):
        validator.validate({**base, "operation": "download_module"})
    bad = {**base, "outcome": "abstain", "reason": "network_error", "reason_detail": "nope"}
    with pytest.raises(ValidationError):
        validator.validate(bad)
    with pytest.raises(ValidationError):
        validator.validate({**base, "source_kind": "remote_git"})


def test_existing_receipts_still_validate_without_source_kind() -> None:
    """``source_kind`` is OPTIONAL: a lookup/resolve receipt that omits it still validates."""
    validator = schemas.validator(schemas.TERRAFORM_GROUNDING_RECEIPT)
    receipt = {
        "schema_version": 1,
        "operation": "lookup_declaration",
        "query": {"address": "aws_instance.web", "module": "infra"},
        "snapshot_digest": "sha256:" + "0" * 64,
        "module_digest": "sha256:" + "0" * 64,
        "backend": {
            "parser": "python-hcl2",
            "parser_version": "8.1.3",
            "analyzer": "rebar-terraform-structural",
            "analyzer_version": 1,
            "config_digest": "sha256:" + "0" * 64,
        },
        "limits": {"modules": 64, "files": 5000, "bytes": 33554432, "timeout_ms": 60000},
        "outcome": "refuted",
        "reason": None,
        "reason_detail": None,
        "result_digest": "sha256:" + "0" * 64,
    }
    validator.validate(receipt)


def test_probe_reason_details_all_map_to_a_generic_reason() -> None:
    """Every probe ``reason_detail`` key is in the closed ABSTENTIONS table and maps to a
    generic reason that is itself in the closed grounding ``ABSTAIN_REASONS`` set."""
    from rebar.grounding import terraform_receipt as tr

    for key in ts.PROBE_REASON_DETAILS:
        assert key in tr.ABSTENTIONS, key
        generic = tr.ABSTENTIONS[key][0]
        assert generic in ev.ABSTAIN_REASONS, (key, generic)


# ── query redaction: no token/header/home path/absolute path in the hashed query ──


def test_probe_query_is_redacted(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TF_TOKEN_registry_example_com", _SECRET)
    _patch_net(monkeypatch, _capture_auth([]))
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.probe_source("registry.example.com/ns/name/aws", from_module="infra")
    finally:
        session.finalize()
    q = res.receipt["query"]
    assert q["operation"] == "probe_source"
    assert q["source"] == "registry.example.com/ns/name/aws"
    assert q["from_module"] == "infra"
    blob = json.dumps(q)
    assert _SECRET not in blob
    assert "Authorization" not in blob and "Bearer" not in blob
    assert str(Path.home()) not in blob


# ── missing extra fails open (no raise) ──


def test_missing_extra_abstains_fail_open(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tft, "available", lambda: False)
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.probe_source("../modules/vpc", from_module="infra")
    finally:
        session.finalize()
    assert res.evidence["outcome"] == ev.OUTCOME_ABSTAIN
    assert res.receipt["reason_detail"] == "missing_extra"
    assert res.receipt["operation"] == "probe_source"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
