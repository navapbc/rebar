"""Guard for the SSM-SecureString-state gate [rebar:eb67-b96c-dcf0-4f86].

An ``aws_ssm_parameter`` with a plaintext ``value`` persists that value in CLEARTEXT in the remote
terraform state -- the provider reads the live value into ``attributes.value`` on every
refresh/import, even under ``lifecycle { ignore_changes = [value] }`` (which only stops terraform
WRITING it). For a ``SecureString`` secret that collapsed the secret tier of the prod state to the
state-bucket ACL (bug eb67: 23 secrets in cleartext). The structural fix (ADR 0105) is write-only
arguments ``value_wo`` + ``value_wo_version``, which the provider never stores to state.

These tests pin the gate's teeth to that exact property: the seeded-secret antipattern is RED, the
write-only form is GREEN, a one-param revert is RED again, and the generated-value case
(``opcert_origin_guard``) stays allowed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from check_ssm_secret_state import Finding, check_repo, scan_text

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "check_ssm_secret_state.py"

# --- HCL fixtures ----------------------------------------------------------------------------- #

_SEEDED_SECRET = (
    'resource "aws_ssm_parameter" "x" {\n'
    '  name  = "/rebar/prod/secret"\n'
    '  type  = "SecureString"\n'
    '  value = "CHANGEME"\n'
    "  lifecycle {\n    ignore_changes = [value]\n  }\n"
    "}\n"
)
_WRITE_ONLY = (
    'resource "aws_ssm_parameter" "x" {\n'
    '  name             = "/rebar/prod/secret"\n'
    '  type             = "SecureString"\n'
    '  value_wo         = "CHANGEME"\n'
    "  value_wo_version = 1\n"
    "}\n"
)
_GENERATED = (
    'resource "aws_ssm_parameter" "g" {\n'
    '  name  = "/rebar/prod/guard"\n'
    '  type  = "SecureString"\n'
    "  value = random_password.g.result\n"
    "}\n"
)
_STRING_PARAM = (
    'resource "aws_ssm_parameter" "p" {\n'
    '  name  = "/rebar/prod/project"\n'
    '  type  = "String"\n'
    '  value = "REB"\n'
    "}\n"
)
_VAR_SOURCED = (
    'resource "aws_ssm_parameter" "x" {\n'
    '  type  = "SecureString"\n'
    "  value = var.gerrit_password\n"
    "}\n"
)
_LOCAL_SOURCED = (
    'resource "aws_ssm_parameter" "x" {\n'
    '  type  = "SecureString"\n'
    "  value = local.rebar_bot_signing_key\n"
    "}\n"
)
_INSECURE_VALUE = (
    'resource "aws_ssm_parameter" "x" {\n'
    '  type           = "SecureString"\n'
    '  insecure_value = "PLACEHOLDER-NOT-A-REAL-SECRET"\n'
    "}\n"
)
# A SecureString whose `description` carries stray braces (a JSON policy snippet). Naive
# brace-counting would truncate the body before `type`/`value` and skip the block entirely.
_BRACE_IN_DESCRIPTION = (
    'resource "aws_ssm_parameter" "x" {\n'
    '  description = "policy {\\"Statement\\": []} tail }{"\n'
    '  type        = "SecureString"\n'
    '  value       = "CHANGEME"\n'
    "  lifecycle {\n    ignore_changes = [value]\n  }\n"
    "}\n"
)


def _probs(text: str) -> list[str]:
    return [f.problem for f in scan_text("t.tf", text)]


# --- the antipattern is RED for the RIGHT reason ---------------------------------------------- #


def test_seeded_secret_literal_value_is_flagged() -> None:
    probs = _probs(_SEEDED_SECRET)
    assert any("string-literal" in p for p in probs), probs


def test_seeded_secret_ignore_changes_is_flagged() -> None:
    assert any("ignore_changes" in p for p in _probs(_SEEDED_SECRET))


# --- RED -> GREEN on the migration, with mutation teeth --------------------------------------- #


def test_write_only_form_is_clean() -> None:
    """The fix: value_wo + value_wo_version draws no finding."""
    assert scan_text("t.tf", _WRITE_ONLY) == []


def test_reverting_one_param_returns_to_red(tmp_path: Path) -> None:
    """Mutation teeth: reverting a migrated param to value + ignore_changes re-arms the gate."""
    tf = tmp_path / "infra" / "terraform"
    tf.mkdir(parents=True)
    (tf / "a.tf").write_text(_WRITE_ONLY)
    assert check_repo(tmp_path) == [], "write-only tree must be GREEN"

    (tf / "a.tf").write_text(_SEEDED_SECRET)
    findings = check_repo(tmp_path)
    assert findings, "reverting one param to value + ignore_changes must be RED"
    assert all(isinstance(f, Finding) for f in findings)


# --- the allowed cases must NOT be flagged ---------------------------------------------------- #


def test_generated_value_is_allowed() -> None:
    """A terraform-GENERATED value (an unquoted expression, no ignore_changes) is out of scope:
    its secret already lives in state via the source resource, so value_wo gives no net benefit.
    This is the opcert_origin_guard case."""
    assert scan_text("t.tf", _GENERATED) == []


def test_non_secret_string_param_is_ignored() -> None:
    """String/StringList params are not secrets; the gate never touches them."""
    assert scan_text("t.tf", _STRING_PARAM) == []


# --- var/local-sourced and insecure_value are secrets that land in state (RED) ---------------- #


def test_var_sourced_value_is_flagged() -> None:
    """`value = var.<x>` is unquoted but NOT generated: it is a real secret terraform reads into
    state, so it must be RED (not mistaken for the allowed random_password case)."""
    probs = _probs(_VAR_SOURCED)
    assert any("var.`/`local." in p for p in probs), probs


def test_local_sourced_value_is_flagged() -> None:
    """`value = local.<x>` is likewise a real secret that lands in state."""
    probs = _probs(_LOCAL_SOURCED)
    assert any("var.`/`local." in p for p in probs), probs


def test_insecure_value_on_securestring_is_flagged() -> None:
    """`insecure_value` is stored to state as PLAINTEXT by design, so it is never a valid value
    source for a SecureString secret."""
    probs = _probs(_INSECURE_VALUE)
    assert any("insecure_value" in p for p in probs), probs


def test_brace_in_description_does_not_truncate_the_block() -> None:
    """Regression: a stray `}`/`{` inside a `description` string must not truncate the parsed body.
    Naive brace-counting would close the block early and skip the SecureString entirely; the
    HCL-aware matcher still sees `type`/`value` and flags the literal + ignore_changes."""
    probs = _probs(_BRACE_IN_DESCRIPTION)
    assert any("string-literal" in p for p in probs), probs
    assert any("ignore_changes" in p for p in probs), probs


def test_value_wo_without_version_is_flagged() -> None:
    body = (
        'resource "aws_ssm_parameter" "x" {\n'
        '  type     = "SecureString"\n'
        '  value_wo = "CHANGEME"\n'
        "}\n"
    )
    assert any("value_wo_version" in p for p in _probs(body))


def test_value_wo_version_without_value_wo_is_flagged() -> None:
    """The mirror branch: a lone `value_wo_version` (no `value_wo`) never triggers the write and is
    an invalid half-migration, so it must be flagged too."""
    body = (
        'resource "aws_ssm_parameter" "x" {\n'
        '  type             = "SecureString"\n'
        "  value_wo_version = 1\n"
        "}\n"
    )
    probs = _probs(body)
    assert any("without `value_wo`" in p for p in probs), probs


def test_securestring_with_no_value_source_is_flagged() -> None:
    body = 'resource "aws_ssm_parameter" "x" {\n  type = "SecureString"\n}\n'
    assert any("no value source" in p for p in _probs(body))


# --- the committed tree + wiring -------------------------------------------------------------- #


def test_committed_tree_is_clean() -> None:
    """The gate is wired into make lint; the committed tree must satisfy it."""
    assert check_repo(REPO_ROOT) == [], [f.render() for f in check_repo(REPO_ROOT)]


def test_gate_exits_zero_on_the_committed_tree() -> None:
    proc = subprocess.run([sys.executable, str(GATE)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_the_three_migrated_resources_are_present_and_write_only() -> None:
    """Pin coverage: the three seeded-secret resources exist and are write-only, so a future
    refactor cannot silently drop one from the tree and pass the committed-tree check vacuously.

    The names below are terraform RESOURCE names (labels the guard asserts on), not secret values;
    each sits on its own line so the deterministic secrets detector does not read the
    resource-label list as a keyword=value secret assignment."""
    tf = (REPO_ROOT / "infra" / "terraform").rglob("*.tf")
    text = "\n".join(p.read_text(encoding="utf-8") for p in tf)
    migrated_resource_names = (
        "rebar_secrets",
        "opcert_ed25519_key",
        "cookie_signing_secret",
    )
    for name in migrated_resource_names:
        assert f'"aws_ssm_parameter" "{name}"' in text, f"missing resource {name}"
    assert "value_wo" in text and "value_wo_version" in text


@pytest.mark.repo_policy
def test_make_lint_wires_the_gate() -> None:
    """A gate reachable only from CI can be green locally and red on the server; wire it into
    make lint like its sibling terraform gate."""
    body = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "scripts/check_ssm_secret_state.py" in body, (
        "`make lint` does not invoke scripts/check_ssm_secret_state.py"
    )
