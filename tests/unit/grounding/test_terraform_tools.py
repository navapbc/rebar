"""Contract oracle for the Terraform structural grounding TOOLS (REB-640 / slice
forcible-diminished-lamb).

These tests fix the OBSERVABLE contract of the per-agent-call Terraform session:
``lookup_declaration`` / ``resolve_reference`` each return a three-valued
``refuted | abstain`` evidence record (never ``match``, never an asserted absence)
plus a canonical, credential-redacting receipt. They assert on tool OUTPUT
(evidence dict, receipt dict, finalized usage) — never on private structure.

The session NEVER executes Terraform, OpenTofu, a provider, or any external
process (slice forcible-diminished-lamb is pure in-process ``python-hcl2`` parsing);
``test_no_forbidden_executables`` is the standing guard for that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar import schemas
from rebar.grounding import evidence as ev

# The extra gates the heavy parser; skip cleanly when it is absent. The module
# under test is imported DIRECTLY (not importorskip) so its absence is a hard RED,
# never a silent skip.
pytest.importorskip("hcl2")
from rebar.grounding import terraform_tools as tft

# ── fixtures: a small, real, repo-contained Terraform module tree ────────────


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with a root module `infra/` that calls a local child `modules/vpc`."""
    _write(
        tmp_path,
        "infra/main.tf",
        'variable "region" {\n'
        "  type    = string\n"
        '  default = "us-east-1"\n'
        "}\n"
        'resource "aws_instance" "web" {\n'
        '  ami           = "ami-123"\n'
        "  instance_type = var.region\n"
        "}\n"
        'module "vpc" {\n'
        '  source = "../modules/vpc"\n'
        "}\n"
        'output "web_id" {\n'
        "  value = aws_instance.web.id\n"
        "}\n",
    )
    _write(
        tmp_path,
        "modules/vpc/main.tf",
        'variable "cidr" {\n  default = "10.0.0.0/16"\n}\n'
        'output "vpc_id" {\n  value = "vpc-abc"\n}\n',
    )
    return tmp_path


def _session(repo: Path, selected):
    return tft.open_session(repo_root=str(repo), selected=list(selected))


# ── HAPPY PATH (given to the implementer) ────────────────────────────────────


def test_lookup_declaration_refutes_absence_of_a_real_resource(repo: Path) -> None:
    """A statically addressable declaration DISPROVES a claim it is absent: the
    evidence is `refuted` (job=refute, tier T1) and points at the definition site."""
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.lookup_declaration("aws_instance.web", module_path="infra")
    finally:
        session.finalize()

    e = res.evidence
    assert e["outcome"] == ev.OUTCOME_REFUTED
    assert e["reason"] is None
    assert e["job"] == ev.JOB_REFUTE
    assert e["provenance_tier"] == ev.TIER_T1
    assert e["reference"]["kind"] == "symbol"
    assert e["reference"]["name"] == "aws_instance.web"
    assert e["reference"]["language"] == "terraform"
    assert e["reference"]["terraform_kind"] == "managed_resource"
    # location is the definition site inside infra/main.tf
    assert e["location"]["file"] == "infra/main.tf"
    assert e["location"]["line_start"] == 5
    # validates against the canonical grounding evidence schema
    schemas.validator(schemas.GROUNDING).validate(e)


def test_lookup_declaration_emits_a_schema_valid_receipt(repo: Path) -> None:
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.lookup_declaration("variable.region", module_path="infra")
    finally:
        session.finalize()

    r = res.receipt
    assert r["operation"] == "lookup_declaration"
    assert r["outcome"] == ev.OUTCOME_REFUTED
    # canonical provenance: normalized query + digests + backend + limits + result digest
    assert r["query"]  # normalized
    for digest_key in ("snapshot_digest", "module_digest", "result_digest"):
        assert r[digest_key].startswith("sha256:")
    assert r["backend"]["parser"] == "python-hcl2"
    assert r["backend"]["parser_version"] == "8.1.3"
    assert r["limits"]["modules"] == 64
    schemas.validator(schemas.TERRAFORM_GROUNDING_RECEIPT).validate(r)


def test_resolve_reference_resolves_a_local_child_output(repo: Path) -> None:
    """`module.vpc.vpc_id` traverses to the child module's declared output — a
    `refuted` member resolution (kind=member)."""
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.resolve_reference("module.vpc.vpc_id", from_file="infra/main.tf")
    finally:
        session.finalize()
    e = res.evidence
    assert e["outcome"] == ev.OUTCOME_REFUTED
    assert e["reference"]["kind"] == "member"
    assert e["reference"]["name"] == "module.vpc.vpc_id"
    schemas.validator(schemas.GROUNDING).validate(e)


# ── HELD-OUT: EDGE / BOUNDARY / ERROR ORACLE (withheld from the implementer) ──


def test_lookup_of_missing_declaration_abstains_no_unique_address(repo: Path) -> None:
    """No matching declaration is NOT an assertion of absence — it abstains."""
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.lookup_declaration("aws_instance.nope", module_path="infra")
    finally:
        session.finalize()
    e = res.evidence
    assert e["outcome"] == ev.OUTCOME_ABSTAIN
    assert e["reason"] == "ambiguous"
    assert res.receipt["reason_detail"] == "no_unique_address"


def test_duplicate_declaration_abstains_duplicate_address(tmp_path: Path) -> None:
    _write(tmp_path, "d/a.tf", 'variable "dup" {\n  default = 1\n}\n')
    _write(tmp_path, "d/b.tf", 'variable "dup" {\n  default = 2\n}\n')
    session = _session(tmp_path, ["d/a.tf"])
    try:
        res = session.lookup_declaration("variable.dup", module_path="d")
    finally:
        session.finalize()
    assert res.evidence["outcome"] == ev.OUTCOME_ABSTAIN
    assert res.evidence["reason"] == "ambiguous"
    assert res.receipt["reason_detail"] == "duplicate_address"


def test_provider_attribute_reference_abstains(repo: Path) -> None:
    """A provider-defined attribute is unknowable statically → abstain, never refuted."""
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.resolve_reference("aws_instance.web.private_ip", from_file="infra/main.tf")
    finally:
        session.finalize()
    assert res.evidence["outcome"] == ev.OUTCOME_ABSTAIN
    assert res.evidence["reason"] == "ambiguous"
    assert res.receipt["reason_detail"] == "provider_attribute"


def test_splat_or_index_reference_abstains(repo: Path) -> None:
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.resolve_reference("aws_instance.web[*].id", from_file="infra/main.tf")
    finally:
        session.finalize()
    assert res.evidence["outcome"] == ev.OUTCOME_ABSTAIN
    assert res.receipt["reason_detail"] == "splat_index"


def test_non_terraform_input_abstains_unsupported_lang(tmp_path: Path) -> None:
    _write(tmp_path, "app/main.py", "print('not terraform')\n")
    session = _session(tmp_path, ["app/main.py"])
    try:
        res = session.lookup_declaration("variable.x", module_path="app")
    finally:
        session.finalize()
    assert res.evidence["outcome"] == ev.OUTCOME_ABSTAIN
    assert res.evidence["reason"] == "unsupported_lang"
    assert res.receipt["reason_detail"] == "not_terraform"


def test_path_escaping_the_repo_abstains_without_asserting_absence(repo: Path) -> None:
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.lookup_declaration("variable.region", module_path="../../etc")
    finally:
        session.finalize()
    e = res.evidence
    assert e["outcome"] == ev.OUTCOME_ABSTAIN
    assert e["reason"] == "private_or_internal_suspected"
    assert res.receipt["reason_detail"] == "path_outside_snapshot"


def test_module_hint_outside_snapshot_does_not_widen_scope(tmp_path: Path) -> None:
    """A ``module_path`` hint naming an in-repo dir that is NOT in the frozen snapshot
    must NOT silently widen the search to every snapshot module. The address exists in
    ``infra`` but the caller scoped the query to ``other`` (out of snapshot), so refuting
    with ``infra``'s declaration would be a false, scope-crossing refutation → abstain."""
    _write(tmp_path, "infra/main.tf", 'resource "aws_instance" "web" {\n  ami = "ami-1"\n}\n')
    # `other/` is in the repo but is NOT selected and is unrelated to infra's closure,
    # so it is not part of the bounded snapshot.
    _write(tmp_path, "other/main.tf", 'variable "unrelated" {\n  default = 1\n}\n')
    session = _session(tmp_path, ["infra/main.tf"])
    try:
        res = session.lookup_declaration("aws_instance.web", module_path="other")
    finally:
        session.finalize()
    e = res.evidence
    assert e["outcome"] == ev.OUTCOME_ABSTAIN, (
        "an out-of-snapshot module hint must not resolve against a different module"
    )
    assert e["reason"] != ev.OUTCOME_REFUTED
    # empty target set for the out-of-snapshot hint → no Terraform in the bounded scope,
    # NOT a scope-crossing refutation off infra's declaration.
    assert res.receipt["reason_detail"] == "not_terraform"


def test_module_hint_outside_snapshot_never_reads_disk(tmp_path: Path) -> None:
    """The frozen snapshot is the SOLE source of files in scope: ``resolve_reference``
    from a ``from_file`` in an in-repo dir absent from the snapshot must never fall back
    to reading ``.tf`` off disk — even when that dir declares the referenced symbol. It
    abstains (never refuting off out-of-snapshot data) and reads nothing there."""
    _write(tmp_path, "infra/main.tf", 'resource "aws_instance" "web" {\n  ami = "ami-1"\n}\n')
    _write(
        tmp_path,
        "secret/main.tf",
        'variable "hidden" {\n  default = "x"\n}\noutput "leak" {\n  value = var.hidden\n}\n',
    )
    session = _session(tmp_path, ["infra/main.tf"])
    try:
        res = session.resolve_reference("var.hidden", from_file="secret/main.tf")
        usage = session.finalize()
    finally:
        pass
    assert res.evidence["outcome"] == ev.OUTCOME_ABSTAIN, (
        "a from_file outside the bounded snapshot must not resolve off disk-read data"
    )
    assert "secret/main.tf" not in usage.concrete_reads, (
        "a dir outside the bounded snapshot must never be read from disk"
    )


def test_declaration_span_correlation_is_per_type_and_document_ordered(tmp_path: Path) -> None:
    """Discriminating oracle for the block-span↔loads-entry correlation: multiple blocks
    of the SAME type, interleaved with other types, must each map to their OWN definition
    span. A mis-correlation (global counter, or order not preserved per type) would return
    a swapped or wrong ``line_start`` for at least one address."""
    _write(
        tmp_path,
        "m/main.tf",
        'resource "aws_instance" "a" {\n'  # line 1
        '  ami = "ami-a"\n'
        "}\n"
        'variable "region" {\n'  # line 4
        '  default = "us-east-1"\n'
        "}\n"
        'resource "aws_instance" "b" {\n'  # line 7
        '  ami = "ami-b"\n'
        '  extra = "x"\n'
        "}\n"
        'data "aws_ami" "c" {\n'  # line 11
        '  owners = ["self"]\n'
        "}\n",
    )
    session = _session(tmp_path, ["m/main.tf"])
    try:
        starts = {
            "aws_instance.a": session.lookup_declaration("aws_instance.a", module_path="m"),
            "aws_instance.b": session.lookup_declaration("aws_instance.b", module_path="m"),
            "variable.region": session.lookup_declaration("variable.region", module_path="m"),
            "data.aws_ami.c": session.lookup_declaration("data.aws_ami.c", module_path="m"),
        }
    finally:
        session.finalize()
    lines = {k: r.evidence["location"]["line_start"] for k, r in starts.items()}
    assert lines["aws_instance.a"] == 1
    assert lines["variable.region"] == 4
    assert lines["aws_instance.b"] == 7
    assert lines["data.aws_ami.c"] == 11
    # the two same-type resources must map to DISTINCT spans (no collapse/duplication)
    assert lines["aws_instance.a"] != lines["aws_instance.b"]


def test_syntax_error_abstains_parse_error(tmp_path: Path) -> None:
    _write(tmp_path, "bad/main.tf", 'resource "aws_x" "y" {\n  this = = broken\n')
    session = _session(tmp_path, ["bad/main.tf"])
    try:
        res = session.lookup_declaration("aws_x.y", module_path="bad")
    finally:
        session.finalize()
    assert res.evidence["outcome"] == ev.OUTCOME_ABSTAIN
    assert res.evidence["reason"] == "parse_error"


def test_abstain_receipt_hashes_only_outcome_reason_detail(repo: Path) -> None:
    """An abstain result_digest must NOT leak the (unresolved) query facts: it is a
    digest of exactly {outcome, reason, reason_detail}."""
    session = _session(repo, ["infra/main.tf"])
    try:
        res = session.lookup_declaration("aws_instance.nope", module_path="infra")
    finally:
        session.finalize()
    r = res.receipt
    from rebar._store.canonical import content_hash

    expected = "sha256:" + content_hash(
        {"outcome": "abstain", "reason": r["reason"], "reason_detail": r["reason_detail"]}
    )
    assert r["result_digest"] == expected


def test_receipt_never_contains_literal_values_or_credentials(tmp_path: Path) -> None:
    """The `default` literal and any token-shaped string must not appear in the receipt."""
    secret = "supersecrettoken-DO-NOT-LEAK"
    _write(
        tmp_path,
        "s/main.tf",
        f'variable "api_key" {{\n  default = "{secret}"\n}}\n',
    )
    session = _session(tmp_path, ["s/main.tf"])
    try:
        res = session.lookup_declaration("variable.api_key", module_path="s")
    finally:
        session.finalize()
    import json

    blob = json.dumps(res.receipt) + json.dumps(res.evidence)
    assert secret not in blob
    assert "10.0.0.0" not in blob  # no literal payloads at all


def test_finalize_reports_concrete_reads_and_membership_globs(repo: Path) -> None:
    session = _session(repo, ["infra/main.tf"])
    session.lookup_declaration("aws_instance.web", module_path="infra")
    usage = session.finalize()
    # concrete reads are the real .tf files parsed, repo-relative
    assert "infra/main.tf" in usage.concrete_reads
    # membership globs protect against a later sibling .tf addition
    assert "infra/**/*.tf" in usage.membership_globs or "**/*.tf" in usage.membership_globs


def test_no_forbidden_executables(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The session parses IN-PROCESS: no subprocess/exec of terraform, opentofu, a
    provider, tfparse, tflint, trivy, terraform-ls or terraform-docs is ever launched."""
    import subprocess

    launched: list = []

    real_popen = subprocess.Popen

    def _spy_popen(cmd, *a, **k):  # pragma: no cover - guard
        launched.append(cmd)
        return real_popen(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", _spy_popen)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: launched.append(a) or None)

    session = _session(repo, ["infra/main.tf"])
    session.lookup_declaration("aws_instance.web", module_path="infra")
    session.resolve_reference("module.vpc.vpc_id", from_file="infra/main.tf")
    session.finalize()

    assert launched == [], f"grounding session must not launch any process, launched: {launched}"
