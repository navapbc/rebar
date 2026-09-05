"""Contract tests for the optional terraform-config-inspect corroborator (REB-640)."""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from rebar import schemas
from rebar.grounding import evidence as ev
from rebar.grounding import terraform_tools as tft

pytest.importorskip("hcl2")


SECRET = "fixture-secret-credential-value"
# mechanism-ok: env_var REBAR_TERRAFORM_CONFIG_INSPECT_CANARY — REB-640 c6ce optional pinned-upstream canary  # noqa: E501
CANARY_ENV = "REBAR_TERRAFORM_CONFIG_INSPECT_CANARY"


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _module_json(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "path": ".",
        "variables": {
            "region": {
                "name": "region",
                "type": "string",
                "description": SECRET,
                "default": SECRET,
                "required": False,
                "sensitive": False,
                "pos": {"filename": "main.tf", "line": 1},
            }
        },
        "outputs": {
            "bucket": {
                "name": "bucket",
                "description": SECRET,
                "sensitive": False,
                "pos": {"filename": "main.tf", "line": 2},
            }
        },
        "managed_resources": {
            "aws_s3_bucket.logs": {
                "mode": "managed",
                "type": "aws_s3_bucket",
                "name": "logs",
                "provider": {"name": "aws"},
                "pos": {"filename": "main.tf", "line": 3},
            }
        },
        "data_resources": {
            "aws_ami.base": {
                "mode": "data",
                "type": "aws_ami",
                "name": "base",
                "provider": {"name": "aws"},
                "pos": {"filename": "main.tf", "line": 4},
            }
        },
        "module_calls": {
            "vpc": {
                "name": "vpc",
                "source": "../modules/vpc",
                "version": None,
                "pos": {"filename": "main.tf", "line": 5},
            }
        },
        "required_providers": {
            "aws": {
                "source": "hashicorp/aws",
                "version_constraints": [">= 5.0"],
                "configuration_aliases": [],
            }
        },
        "required_core": [],
        "provider_configs": {},
        "diagnostics": [],
    }
    data.update(overrides)
    return data


def _fixture_binary(
    bin_dir: Path, capture: Path, payload: dict[str, Any], *, mode: str = "ok"
) -> Path:
    exe = bin_dir / "terraform-config-inspect"
    exe.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json, os, pathlib, stat, sys, time
            capture = pathlib.Path({str(capture)!r})
            stdin = sys.stdin.read()
            cwd = pathlib.Path.cwd()
            files = []
            for p in cwd.rglob("*"):
                if p.is_symlink():
                    kind = "symlink"
                elif p.is_file():
                    kind = "file"
                elif p.is_dir():
                    kind = "dir"
                else:
                    kind = "other"
                files.append({{
                    "rel": p.relative_to(cwd).as_posix(),
                    "kind": kind,
                    "mode": stat.S_IMODE(p.lstat().st_mode),
                }})
            capture.write_text(json.dumps({{
                "argv": sys.argv,
                "cwd": str(cwd),
                "env": dict(os.environ),
                "stdin": stdin,
                "files": files,
            }}, sort_keys=True), encoding="utf-8")
            if {mode!r} == "hang":
                time.sleep(10)
            elif {mode!r} == "overflow":
                sys.stdout.write("x" * (4 * 1024 * 1024 + 2))
            elif {mode!r} == "nonzero":
                sys.stderr.write("child stream {SECRET}\\n")
                sys.exit(1)
            elif {mode!r} == "replace":
                current = pathlib.Path(sys.argv[0]).read_text(encoding="utf-8")
                pathlib.Path(sys.argv[0]).write_text(
                    current + "\\n# replaced\\n",
                    encoding="utf-8",
                )
                print(json.dumps({payload!r}))
            else:
                sys.stderr.write("child stream {SECRET}\\n")
                print(json.dumps({payload!r}))
            """
        ),
        encoding="utf-8",
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def _session(repo: Path) -> tft.TerraformSession:
    return tft.open_session(repo_root=str(repo), selected=["infra/main.tf"])


def _corroborate(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any] | None = None,
    *,
    mode: str = "ok",
    diagnostic: str = "declaration_present",
    subject: str = "variable.region",
    expected: str = "",
) -> tuple[Any, dict[str, Any], Path]:
    bin_dir = repo.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    capture = repo.parent / "capture.json"
    _fixture_binary(bin_dir, capture, payload or _module_json(), mode=mode)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("TF_TOKEN_registry_terraform_io", SECRET)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRET)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    monkeypatch.setenv("TF_CLI_CONFIG_FILE", str(repo / ".terraformrc"))
    session = _session(repo)
    try:
        result = session.corroborate_diagnostic(
            "infra", diagnostic=diagnostic, subject=subject, expected=expected
        )
    finally:
        session.finalize()
    captured = json.loads(capture.read_text(encoding="utf-8")) if capture.exists() else {}
    return result, captured, capture


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _write(
        tmp_path,
        "infra/main.tf",
        """
        variable "region" {}
        output "bucket" { value = "redacted" }
        resource "aws_s3_bucket" "logs" { bucket = "redacted" }
        data "aws_ami" "base" {}
        module "vpc" { source = "../modules/vpc" }
        terraform { required_providers { aws = { source = "hashicorp/aws" } } }
        """,
    )
    (tmp_path / "infra" / "escape.tf").symlink_to(tmp_path / "infra" / "main.tf")
    return tmp_path


@pytest.mark.parametrize(
    ("subject", "klass"),
    [
        ("variable.region", "variable"),
        ("output.bucket", "output"),
        ("aws_s3_bucket.logs", "managed_resource"),
        ("data.aws_ami.base", "data_resource"),
        ("module.vpc", "module_call"),
        ("required_provider.aws", "required_provider"),
    ],
)
def test_exact_declaration_matches_every_supported_class(
    repo: Path, monkeypatch: pytest.MonkeyPatch, subject: str, klass: str
) -> None:
    result, _capture, _ = _corroborate(repo, monkeypatch, subject=subject)
    assert result.evidence["outcome"] == ev.OUTCOME_MATCH
    assert result.evidence["job"] == ev.JOB_APPLIES
    assert result.evidence["provenance_tier"] == ev.TIER_T1
    assert f"class={klass}" in result.evidence["detail"]
    assert result.receipt["operation"] == "corroborate_diagnostic"
    assert result.receipt["outcome"] == "match"
    assert result.receipt["reason"] is None
    assert result.receipt["reason_detail"] is None
    schemas.validator(schemas.GROUNDING).validate(result.evidence)
    schemas.validator(schemas.TERRAFORM_GROUNDING_RECEIPT).validate(result.receipt)


def test_module_source_equals_matches_by_hash_not_literal(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _capture, _ = _corroborate(
        repo,
        monkeypatch,
        diagnostic="module_source_equals",
        subject="module.vpc",
        expected="../modules/vpc",
    )
    assert result.evidence["outcome"] == ev.OUTCOME_MATCH
    encoded = json.dumps({"e": result.evidence, "r": result.receipt}, sort_keys=True)
    assert "../modules/vpc" not in encoded
    assert "expected_digest" in result.receipt["query"]


@pytest.mark.parametrize(
    ("diagnostic", "subject", "expected", "detail"),
    [
        ("declaration_present", "variable.missing", "", "no_unique_address"),
        ("module_source_equals", "module.vpc", "./other", "computed_value"),
        ("required_provider_present", "google", "", "no_unique_address"),
        ("declaration_absent", "variable.region", "", "invalid_detector"),
        ("declaration_present", "aws_s3_bucket.logs.id", "", "provider_attribute"),
    ],
)
def test_missing_unequal_absence_and_computed_controls_abstain(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    diagnostic: str,
    subject: str,
    expected: str,
    detail: str,
) -> None:
    result, _capture, _ = _corroborate(
        repo, monkeypatch, diagnostic=diagnostic, subject=subject, expected=expected
    )
    assert result.evidence["outcome"] == ev.OUTCOME_ABSTAIN
    assert result.receipt["reason_detail"] == detail


def test_subprocess_contract_strips_environment_and_copies_readonly_snapshot(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, capture, _ = _corroborate(repo, monkeypatch)
    assert result.evidence["outcome"] == ev.OUTCOME_MATCH
    assert Path(capture["argv"][0]).name == "terraform-config-inspect"
    # The directory argument is "." — the child's cwd IS the snapshot, so this names the same
    # directory while keeping the snapshot path out of argv and keeping the tool's pos.filename
    # relative (bug f95d-19f6-7e58-4a8e).
    assert capture["argv"][1:] == ["--json", "."]
    assert Path(capture["cwd"]).name.startswith(".rebar-tfci-")
    assert capture["stdin"] == ""
    assert not any(str(repo) in arg for arg in capture["argv"])
    assert str(repo) not in capture["cwd"]
    assert "TF_TOKEN_registry_terraform_io" not in capture["env"]
    assert "AWS_SECRET_ACCESS_KEY" not in capture["env"]
    assert "HTTPS_PROXY" not in capture["env"]
    assert "TF_CLI_CONFIG_FILE" not in capture["env"]
    assert {"HOME", "TMPDIR"} <= set(capture["env"])
    copied = {f["rel"]: f for f in capture["files"]}
    assert "main.tf" in copied
    assert "escape.tf" not in copied
    assert copied["main.tf"]["mode"] & stat.S_IWUSR == 0
    # The receipt records only the tool BASENAME, never the resolved absolute path (which
    # would leak the operator's home directory / username) — the digest-only redaction posture.
    exe_abs = str(repo.parent / "bin" / "terraform-config-inspect")
    assert result.receipt["executable"]["path"] == "terraform-config-inspect"
    assert exe_abs not in json.dumps(result.receipt, sort_keys=True)


def test_match_receipt_requires_executable_and_invocation_provenance(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _capture, _ = _corroborate(repo, monkeypatch)
    receipt = result.receipt
    assert receipt["outcome"] == "match"
    validator = schemas.validator(schemas.TERRAFORM_GROUNDING_RECEIPT)
    # Baseline: a real match receipt carries executable + invocation provenance and validates.
    assert validator.is_valid(receipt)
    # A `match` receipt stripped of either provenance block is REJECTED (positive corroboration
    # must be reproducible: which audited binary ran, and under exactly what invocation).
    for missing in ("executable", "invocation"):
        stripped = {k: v for k, v in receipt.items() if k != missing}
        assert not validator.is_valid(stripped)


@pytest.mark.parametrize(
    ("mode", "detail"),
    [
        ("hang", "worker_timeout"),
        ("overflow", "worker_failure"),
        ("nonzero", "nonzero_exit"),
        ("replace", "binary_replaced"),
    ],
)
def test_faults_abstain_redact_cleanup_and_retry(
    repo: Path, monkeypatch: pytest.MonkeyPatch, mode: str, detail: str
) -> None:
    from rebar.grounding import terraform_corroborator as tc

    monkeypatch.setattr(tc, "DEADLINE_SECONDS", 0.2)
    result, _capture, _ = _corroborate(repo, monkeypatch, mode=mode)
    assert result.evidence["outcome"] == ev.OUTCOME_ABSTAIN
    assert result.receipt["reason_detail"] == detail
    encoded = json.dumps({"e": result.evidence, "r": result.receipt}, sort_keys=True)
    assert SECRET not in encoded
    assert not list(repo.parent.glob(".rebar-tfci-*"))

    retry, _capture, _ = _corroborate(repo, monkeypatch)
    assert retry.evidence["outcome"] == ev.OUTCOME_MATCH


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        (
            _module_json(diagnostics=[{"severity": "error", "summary": SECRET}]),
            "upstream_diagnostics",
        ),
        ({**_module_json(), "unknown": True}, "config_inspect_schema_skew"),
        (
            _module_json(
                variables={
                    "region": {
                        "name": "region",
                        "pos": {"filename": "../x.tf", "line": 1},
                    }
                }
            ),
            "path_outside_snapshot",
        ),
        ({"not": "json-shape"}, "config_inspect_schema_skew"),
    ],
)
def test_schema_diagnostics_and_path_skew_abstain_without_partial_facts(
    repo: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], detail: str
) -> None:
    result, _capture, _ = _corroborate(repo, monkeypatch, payload=payload)
    assert result.evidence["outcome"] == ev.OUTCOME_ABSTAIN
    assert result.receipt["reason_detail"] == detail
    assert result.evidence.get("location") is None
    assert SECRET not in json.dumps({"e": result.evidence, "r": result.receipt}, sort_keys=True)


def test_missing_binary_is_fail_open_no_tool(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(repo / "missing-bin"))
    session = _session(repo)
    try:
        result = session.corroborate_diagnostic("infra", "declaration_present", "variable.region")
    finally:
        session.finalize()
    assert result.evidence["outcome"] == ev.OUTCOME_ABSTAIN
    assert result.receipt["reason"] == "no_tool"
    assert result.receipt["reason_detail"] == "executable_not_resolvable"


def test_rejects_repo_contained_executable(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = repo / "bin"
    bin_dir.mkdir()
    _fixture_binary(bin_dir, repo.parent / "capture.json", _module_json())
    monkeypatch.setenv("PATH", str(bin_dir))
    session = _session(repo)
    try:
        result = session.corroborate_diagnostic("infra", "declaration_present", "variable.region")
    finally:
        session.finalize()
    assert result.receipt["reason_detail"] == "rejected_executable"


def test_pinned_upstream_canary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get(CANARY_ENV) != "1":
        pytest.skip("set REBAR_TERRAFORM_CONFIG_INSPECT_CANARY=1 to run the pinned canary")
    has_binary = any(
        (Path(p) / "terraform-config-inspect").is_file()
        for p in os.environ["PATH"].split(os.pathsep)
    )
    if not has_binary:
        pytest.fail(
            f"{CANARY_ENV}=1 but terraform-config-inspect is not on PATH — the pinned-upstream "
            "canary lane MUST run against the real audited binary. A missing binary here means "
            "`go install` silently failed; skipping would let the canary pass vacuously."
        )
    repo = tmp_path
    _write(
        repo,
        "infra/main.tf",
        'variable "region" {}\nmodule "vpc" { source = "../modules/vpc" }\n',
    )
    session = _session(repo)
    try:
        positive = session.corroborate_diagnostic("infra", "declaration_present", "variable.region")
        absent = session.corroborate_diagnostic("infra", "declaration_present", "variable.missing")
    finally:
        session.finalize()
    assert positive.evidence["outcome"] == ev.OUTCOME_MATCH
    assert absent.evidence["outcome"] == ev.OUTCOME_ABSTAIN
