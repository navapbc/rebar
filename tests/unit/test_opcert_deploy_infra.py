"""Hermetic infra-wiring contract tests for the op-cert deploy edge (story 76d2).

NO real AWS / network / terraform apply. These pin the Terraform SOURCE, the compose/nginx
wiring, the guard materialize script (exercised with a STUB `aws` on PATH returning a fixture),
and the trusted-environment pinning — the offline half of ACs 1-4 (the live half, AC5, is the
operator's, gated by infra/terraform/opcert-plan-assertions.sh which needs real AWS creds).
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from rebar.attest import trusted_env

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_TF = _REPO / "infra" / "terraform" / "opcert.tf"
_VERSIONS = _REPO / "infra" / "terraform" / "versions.tf"
_ASSERT_SH = _REPO / "infra" / "terraform" / "opcert-plan-assertions.sh"
_COMPOSE = _REPO / "infra" / "compose" / "docker-compose.yml"
_NGINX = _REPO / "infra" / "nginx" / "rebar.conf.template"
_MATERIALIZE = _REPO / "infra" / "scripts" / "materialize-opcert-guard.sh"
_COMPOSE_UP = _REPO / "infra" / "scripts" / "compose-up.sh"
_TRUSTED_ENV = _REPO / ".rebar" / "trusted_environments.yaml"


# --------------------------------------------------------------------------- Terraform source


def test_terraform_opcert_integration_uri_is_https() -> None:
    """AC1(a): the integration proxies to the box origin over HTTPS (not http://:80)."""
    tf = _TF.read_text()
    m = re.search(r'integration_uri\s*=\s*"([^"]+)"', tf)
    assert m, "opcert.tf has no integration_uri"
    assert m.group(1).startswith("https://"), m.group(1)


def test_terraform_opcert_injects_guard_header() -> None:
    """AC1(b): the integration injects the static X-Opcert-Guard origin-guard header."""
    tf = _TF.read_text()
    assert '"append:header.X-Opcert-Guard"' in tf
    # the header value is the Terraform-managed random_password (same value stored in SSM).
    assert "random_password.opcert_guard.result" in tf


def test_terraform_both_securestring_params_present() -> None:
    """AC1(c+d): both /rebar/prod/ params exist as SecureString, key guarded / guard not."""
    tf = _TF.read_text()
    for label, name in (
        ("opcert_ed25519_key", "/rebar/prod/opcert-ed25519-key"),
        ("opcert_origin_guard", "/rebar/prod/opcert-origin-guard"),
    ):
        block = _resource_block(tf, "aws_ssm_parameter", label)
        assert block is not None, f"missing aws_ssm_parameter.{label}"
        assert f'name  = "{name}"' in block or f'name = "{name}"' in block
        assert 'type  = "SecureString"' in block or 'type = "SecureString"' in block

    # AC1(f): the KEY param is guarded by ignore_changes = [value]; the guard param is NOT.
    key_block = _resource_block(tf, "aws_ssm_parameter", "opcert_ed25519_key")
    assert re.search(r"ignore_changes\s*=\s*\[\s*value\s*\]", key_block), (
        "key param lacks ignore_changes"
    )
    guard_block = _resource_block(tf, "aws_ssm_parameter", "opcert_origin_guard")
    assert "ignore_changes" not in guard_block, (
        "guard param must NOT ignore_changes (Terraform-managed value)"
    )


def test_terraform_route_is_sigv4_aws_iam() -> None:
    """AC1(e): the route is SigV4-authenticated (authorization_type = AWS_IAM)."""
    tf = _TF.read_text()
    route = _resource_block(tf, "aws_apigatewayv2_route", "opcert")
    assert route is not None
    assert 'authorization_type = "AWS_IAM"' in route


def test_terraform_random_provider_pinned() -> None:
    """AC1(g): the hashicorp/random provider is in versions.tf required_providers."""
    versions = _VERSIONS.read_text()
    assert "hashicorp/random" in versions
    assert re.search(r"random\s*=\s*{", versions)


def test_terraform_invoke_policy_is_the_sole_grantee() -> None:
    """AC1 invoke-policy: exactly one execute-api:Invoke policy, labelled opcert_admin_invoke,
    on the IaC-created rebar-opcert-admin role, scoped to this API's execution ARN."""
    tf = _TF.read_text()
    # role exists with the pinned name
    role = _resource_block(tf, "aws_iam_role", "opcert_admin")
    assert role is not None and 'name               = "rebar-opcert-admin"' in role
    # the invoke policy carries the pinned label and grants execute-api:Invoke on the API arn
    assert 'resource "aws_iam_role_policy" "opcert_admin_invoke"' in tf
    invoke_doc = _resource_block(tf, "aws_iam_policy_document", "opcert_admin_invoke", data=True)
    assert invoke_doc is not None
    assert '"execute-api:Invoke"' in invoke_doc
    assert "aws_apigatewayv2_api.opcert.execution_arn" in invoke_doc
    # the quoted action appears EXACTLY once in the file (comments use backticks) — one grantee
    assert tf.count('"execute-api:Invoke"') == 1, (
        "execute-api:Invoke must be granted by exactly one policy"
    )
    # the deploy variable that scopes the trust policy exists
    assert 'variable "opcert_admin_principal_arns"' in tf
    assume = _resource_block(tf, "aws_iam_policy_document", "opcert_admin_assume", data=True)
    assert assume is not None and "var.opcert_admin_principal_arns" in assume


def test_terraform_opcert_admin_role_ignores_assume_role_policy_drift() -> None:
    """Regression (bug 7c91-0488): the rebar-opcert-admin role's trust principals are
    operator-supplied at DEPLOY (`var.opcert_admin_principal_arns`, default []). The
    terraform-drift check runs `terraform plan` with NO -var, so it renders an empty
    principal and reports a permanent phantom `1 to change` against the live account-root
    principal — reddening the daily sweep forever and masking real drift. The role must carry
    `lifecycle { ignore_changes = [assume_role_policy] }` (mirroring the operator-seeded key
    param's ignore_changes = [value]) so plan/apply never diff on the operator-owned trust
    list. Same class as the key-param guard asserted in
    test_terraform_both_securestring_params_present."""
    tf = _TF.read_text()
    role = _resource_block(tf, "aws_iam_role", "opcert_admin")
    assert role is not None, "missing aws_iam_role.opcert_admin"
    assert re.search(r"ignore_changes\s*=\s*\[\s*assume_role_policy\s*\]", role), (
        "opcert_admin role must declare lifecycle { ignore_changes = [assume_role_policy] } "
        "so the operator-owned trust policy does not register as terraform-drift"
    )


def test_terraform_no_fixed_cost_resources() -> None:
    """The zero-fixed-cost posture: no Fargate/ECS/LB/Secrets-Manager/KMS-CMK/kms:Sign."""
    tf = _strip_hcl_comments(_TF.read_text())
    for forbidden in (
        "aws_ecs_service",
        "aws_lb",
        "aws_apigatewayv2_vpc_link",
        "aws_secretsmanager_secret",
        "aws_kms_key",
        "kms:Sign",
    ):
        assert forbidden not in tf, f"zero-fixed-cost violation: {forbidden}"


# --------------------------------------------------------------------------- assertion script


def test_assertion_script_present_executable_and_literal() -> None:
    """AC2: the operator assertion script exists, is executable, and carries the literal
    AC1 jq queries (aggregated all() on route auth; the .lifecycle_meta_arguments (f) query)."""
    assert _ASSERT_SH.is_file()
    assert os.access(_ASSERT_SH, os.X_OK), "assertion script must be executable"
    body = _ASSERT_SH.read_text()
    assert 'startswith("https://")' in body
    assert 'has("append:header.X-Opcert-Guard")' in body
    assert 'all(.t == "SecureString")' in body
    assert 'all(. == "AWS_IAM")' in body  # aggregated route auth
    assert 'has("random")' in body
    assert 'label == "opcert_admin_invoke"' in body
    assert ".lifecycle_meta_arguments.ignore_changes" in body  # (f) literal path


# --------------------------------------------------------------------------- compose service


def test_compose_has_opcert_service_with_env_wiring() -> None:
    """AC3: docker-compose has the opcert service with the env wiring + loopback port."""
    doc = yaml.safe_load(_COMPOSE.read_text())
    svc = doc["services"]["opcert"]
    env = svc["environment"]
    assert env["REBAR_OPCERT_SSM_KEY_PARAM"] == "/rebar/prod/opcert-ed25519-key"
    assert str(env["REBAR_SYNC_PUSH"]) == "off"
    assert "REBAR_OPCERT_ENV_ID" in env
    # reached via the HOST nginx /opcert/ proxy on loopback 8090 (Gerrit owns 8080)
    assert any("127.0.0.1:8090:8080" in str(p) for p in svc["ports"])
    assert svc["build"]["dockerfile"] == "infra/compose/Dockerfile.opcert"


# --------------------------------------------------------------------------- nginx template


def test_nginx_template_has_failclosed_guard_map_and_403_block() -> None:
    """AC3: the TEMPLATE itself carries the fail-closed guard map (default 0 + zero-match glob
    include) and the guarded /opcert/ 403 location."""
    conf = _NGINX.read_text()
    # the map: fail-closed default + glob include (tolerates zero matches on a fresh boot)
    assert re.search(r"map\s+\$http_x_opcert_guard\s+\$opcert_guard_ok\s*{", conf), (
        "guard map missing"
    )
    assert "default 0;" in conf
    assert "include /etc/nginx/opcert-guard*.conf;" in conf  # glob => zero-match-safe
    # the guarded location returns 403 when the guard did not match, then proxies to the service
    loc = conf.split("location /opcert/", 1)
    assert len(loc) == 2, "/opcert/ location missing"
    after = loc[1]
    assert re.search(r"if\s*\(\$opcert_guard_ok\s*=\s*0\)\s*{\s*return 403;", after)
    assert "proxy_pass http://127.0.0.1:8090;" in after  # no trailing slash => prefix preserved


# --------------------------------------------------------------------------- materialize script


def test_materialize_script_present_and_wired() -> None:
    """AC3: the guard materialize script exists, ends with nginx -s reload, reads the SSM param,
    and is wired into compose-up.sh BEFORE `docker compose up`."""
    assert _MATERIALIZE.is_file() and os.access(_MATERIALIZE, os.X_OK)
    body = _MATERIALIZE.read_text()
    assert "aws ssm get-parameter" in body
    assert "/rebar/prod/opcert-origin-guard" in body
    assert "nginx -s reload" in body

    up = _COMPOSE_UP.read_text()
    assert "materialize-opcert-guard.sh" in up
    # ordering: the materialize call precedes the stack bring-up (`docker compose ... up -d`)
    idx_mat = up.index("materialize-opcert-guard.sh")
    idx_up = up.index('docker compose -f "${COMPOSE_FILE}" up')
    assert idx_mat < idx_up, "materialize must run BEFORE docker compose up"


def test_materialize_script_renders_guard_from_ssm_stub(tmp_path: Path) -> None:
    """AC3 (hermetic behavior): with a STUB `aws` on PATH returning a fixture, the script writes
    the exact one-line nginx map entry `"<value>" 1;` and lands REBAR_OPCERT_GUARD in the .env —
    no live SSM / account access."""
    fixture = "FIXTURE-GUARD-abc123"
    # stub aws: print the fixture for the SSM get-parameter --output text call
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub_aws = stub_dir / "aws"
    stub_aws.write_text(f'#!/bin/sh\nprintf "%s\\n" "{fixture}"\n')
    stub_aws.chmod(stub_aws.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)

    map_file = tmp_path / "opcert-guard.map.conf"
    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING=1\nREBAR_OPCERT_GUARD=STALE\n")

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env["NGINX_MAP_FILE"] = str(map_file)
    env["ENV_FILE"] = str(env_file)
    env["RELOAD_NGINX"] = "0"  # no host nginx in the test

    res = subprocess.run(
        ["bash", str(_MATERIALIZE)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr

    # the nginx map entry is exactly `"<value>" 1;`
    assert map_file.read_text().strip() == f'"{fixture}" 1;'
    # the .env has the fresh guard (stale line replaced) and preserves other lines
    env_body = env_file.read_text()
    assert f"REBAR_OPCERT_GUARD={fixture}" in env_body
    assert "REBAR_OPCERT_GUARD=STALE" not in env_body
    assert "EXISTING=1" in env_body


def test_materialize_script_fails_closed_on_empty_guard(tmp_path: Path) -> None:
    """AC3: an empty/None SSM value makes the script exit non-zero (fail-closed)."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub_aws = stub_dir / "aws"
    stub_aws.write_text('#!/bin/sh\nprintf "None\\n"\n')  # SSM returns the None sentinel
    stub_aws.chmod(stub_aws.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env["NGINX_MAP_FILE"] = str(tmp_path / "map.conf")
    env["ENV_FILE"] = str(tmp_path / ".env")
    env["RELOAD_NGINX"] = "0"
    res = subprocess.run(
        ["bash", str(_MATERIALIZE)], env=env, capture_output=True, text=True, check=False
    )
    assert res.returncode != 0
    assert not (tmp_path / "map.conf").exists(), "no map file must be written on a failed fetch"


# --------------------------------------------------------------------------- trusted-env pin


def test_trusted_env_pins_deployed_environment_log_position_form() -> None:
    """AC4: the trusted-env entry parses via the real loader, uses the LOG-POSITION era form
    (not the superseded added_at_commit main-SHA form), and its env_id matches the compose
    service's REBAR_OPCERT_ENV_ID."""
    # log-position era form only — the legacy main-SHA field is rejected by the loader.
    # Scan the NON-COMMENT body (the docstring/comments explain the legacy field by name).
    body = "\n".join(
        line for line in _TRUSTED_ENV.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    assert "added_at_log_position" in body
    assert "added_at_commit" not in body

    # parses via the production loader (which also enforces the schema)
    data = trusted_env.load_trusted_environments(str(_REPO))
    assert data is not None
    envs = data["environments"]
    assert len(envs) >= 1
    pinned = envs[0]
    keyring = pinned["keys"]
    assert keyring and keyring[0]["added_at_log_position"]
    assert keyring[0]["public_key"].startswith("ssh-ed25519 ")

    # env_id matches the compose service default REBAR_OPCERT_ENV_ID
    compose = yaml.safe_load(_COMPOSE.read_text())
    raw_env_id = compose["services"]["opcert"]["environment"]["REBAR_OPCERT_ENV_ID"]
    m = re.search(r":-([^}]+)}", raw_env_id)  # ${REBAR_OPCERT_ENV_ID:-<default>}
    compose_env_id = m.group(1) if m else raw_env_id
    assert pinned["env_id"] == compose_env_id


# --------------------------------------------------------------------------- helpers


def _strip_hcl_comments(tf: str) -> str:
    """Drop `#`/`//` line comments so a check scans real config, not prose. (No `#`/`//` appears
    inside a string literal in opcert.tf, so a simple per-line strip is safe here.)"""
    out = []
    for line in tf.splitlines():
        for marker in ("#", "//"):
            idx = line.find(marker)
            if idx != -1:
                line = line[:idx]
        out.append(line)
    return "\n".join(out)


def _resource_block(tf: str, kind: str, label: str, *, data: bool = False) -> str | None:
    """Return a `resource "<kind>" "<label>" { ... }` (or `data`) block, brace-matched."""
    prefix = "data" if data else "resource"
    start = tf.find(f'{prefix} "{kind}" "{label}"')
    if start == -1:
        return None
    brace = tf.find("{", start)
    depth = 0
    for i in range(brace, len(tf)):
        if tf[i] == "{":
            depth += 1
        elif tf[i] == "}":
            depth -= 1
            if depth == 0:
                return tf[start : i + 1]
    return None


def test_dockerfile_opcert_uses_ssh_dash_v_for_version_check() -> None:
    """Regression (bug accc): the OpenSSH >= 8.9 build assertion must read the version from
    `ssh -V` (which prints `OpenSSH_X.Y...` to stderr), NOT `ssh-keygen -V` (a validity-interval
    flag that errors without an argument, yielding an empty version so the check ALWAYS fails and
    the image never builds)."""
    dockerfile = (_REPO / "infra" / "compose" / "Dockerfile.opcert").read_text(encoding="utf-8")
    assert "ssh -V 2>&1" in dockerfile, "OpenSSH version must be read from `ssh -V`"
    assert "ssh-keygen -V 2>&1" not in dockerfile, "must not use `ssh-keygen -V` (wrong flag)"


def test_nginx_template_raises_map_hash_bucket_size_for_guard() -> None:
    """Regression (bug accc): the op-cert guard value is a 48-char token longer than nginx's
    default 64-byte map-hash bucket, so the template must raise `map_hash_bucket_size` — else
    `nginx -t` fails with 'could not build map_hash' and the /opcert/ route can never load."""
    tmpl = (_REPO / "infra" / "nginx" / "rebar.conf.template").read_text(encoding="utf-8")
    assert re.search(r"map_hash_bucket_size\s+\d+\s*;", tmpl), (
        "template must set map_hash_bucket_size"
    )
    # and it must appear before the guard map block it protects
    idx_size = tmpl.find("map_hash_bucket_size")
    idx_map = tmpl.find("map $http_x_opcert_guard")
    assert 0 <= idx_size < idx_map, "map_hash_bucket_size must precede the guard map block"


def test_opcert_compose_service_sets_aws_region() -> None:
    """Regression (bug accc): the opcert service's boto3 SSM key fetch needs a region — the
    instance profile supplies credentials via IMDS but the region is not auto-discovered inside
    the container, so the compose service must set AWS_REGION (else every job fails 'You must
    specify a region')."""
    import yaml as _yaml

    compose = _yaml.safe_load(
        (_REPO / "infra" / "compose" / "docker-compose.yml").read_text(encoding="utf-8")
    )
    env = compose["services"]["opcert"].get("environment", {})
    # environment may be a dict or a list of "K=V"; normalize to a key set
    keys = set(env) if isinstance(env, dict) else {e.split("=", 1)[0] for e in env}
    assert "AWS_REGION" in keys, "opcert service must set AWS_REGION for the boto3 SSM client"


def test_terraform_ci_runs_fmt_check_and_validate() -> None:
    """Story 76d2: the terraform CI workflow must run `terraform fmt -check` AND
    `terraform validate` (not only `plan`), so a mis-formatted or invalid .tf module fails CI."""
    wf = (_REPO / ".github" / "workflows" / "terraform-drift.yml").read_text(encoding="utf-8")
    assert "terraform fmt -check" in wf, "CI must run `terraform fmt -check`"
    assert "terraform validate" in wf, "CI must run `terraform validate`"
