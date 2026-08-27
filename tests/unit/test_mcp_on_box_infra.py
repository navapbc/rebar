"""Hermetic infra-wiring contract tests for the rebar MCP-on-box edge (ADR
deft-evolutive-mosasaur / docs/adr/0104-mcp-on-box.md; story cibophobic-holohedral-esok).

NO docker / AWS / network. These pin the OFFLINE half of the ACs: the new `mcp` compose
service (posture env, loopback port, healthcheck, stop_grace_period), the Dockerfile.mcp
build recipe, the nginx switchable `upstream rebar_mcp` + `/mcp` location, the committed
upstream seed, and the fresh-boot installer wired into compose-up.sh. The live AWS deploy
(approval-gated) is the operator's and out of scope here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_COMPOSE = _REPO / "infra" / "compose" / "docker-compose.yml"
_DOCKERFILE = _REPO / "infra" / "compose" / "Dockerfile.mcp"
_NGINX = _REPO / "infra" / "nginx" / "rebar.conf.template"
_SEED = _REPO / "infra" / "nginx" / "mcp-upstream.conf"
_MATERIALIZE = _REPO / "infra" / "scripts" / "materialize-mcp-upstream.sh"
_COMPOSE_UP = _REPO / "infra" / "scripts" / "compose-up.sh"
_AUTODEPLOY = _REPO / "infra" / "scripts" / "autodeploy.sh"
_FETCH_SECRETS = _REPO / "infra" / "scripts" / "fetch-secrets.sh"
_ENTRYPOINT = _REPO / "infra" / "scripts" / "mcp-entrypoint.sh"
_DOCKERIGNORE = _REPO / ".dockerignore"
_SSM_TF = _REPO / "infra" / "terraform" / "ssm.tf"
_REBAR_TOML = _REPO / "rebar.toml"

# The app's bounded SIGTERM grace, single-sourced in the module — the compose
# stop_grace_period must cover it.
from rebar._mcp_health import DEFAULT_SHUTDOWN_GRACE_SECONDS  # noqa: E402


def _duration_to_seconds(value: str) -> int:
    """Parse a compose duration like '1260s' / '21m' into seconds (the forms used here)."""
    text = str(value).strip()
    m = re.fullmatch(r"(\d+)\s*([smh]?)", text)
    assert m, f"unrecognised duration {value!r}"
    n = int(m.group(1))
    return {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)] * n


# --------------------------------------------------------------------------- compose service


def test_compose_has_mcp_service_with_http_posture_env() -> None:
    """AC1/AC2 wiring: the `mcp` service builds from Dockerfile.mcp and sets the
    fail-closed HTTP posture env the ADR decided (transport, TLS-at-edge, allowlists,
    static auth)."""
    doc = yaml.safe_load(_COMPOSE.read_text())
    svc = doc["services"]["mcp"]
    assert svc["build"]["context"] == "../.."
    assert svc["build"]["dockerfile"] == "infra/compose/Dockerfile.mcp"
    assert svc["restart"] == "always"
    env = svc["environment"]
    assert env["REBAR_MCP_TRANSPORT"] == "http"
    assert env["REBAR_MCP_HTTP_PORT"] == "8091"
    # TLS-at-edge ack + allowlists (the non-loopback fail-closed gate inputs).
    assert env["REBAR_MCP_HTTP_TLS_AT_EDGE"] == "true"
    assert "REBAR_MCP_HTTP_ALLOWED_HOSTS" in env
    assert "REBAR_MCP_HTTP_ALLOWED_ORIGINS" in env
    # Static bearer auth at /mcp; resource-server url advertised.
    assert env["REBAR_MCP_AUTH_ENABLED"] == "1"
    assert env["REBAR_MCP_AUTH_STRATEGIES"] == "static"
    assert "REBAR_MCP_AUTH_RESOURCE_SERVER_URL" in env


def test_compose_mcp_advertises_a_non_empty_issuer_url() -> None:
    """Regression (haughty-leisured-puffer): with auth enabled the server constructs
    ``AuthSettings(issuer_url=...)`` which pydantic rejects when empty, so the compose
    env MUST define a non-empty ``REBAR_MCP_AUTH_ISSUER_URL`` (it has no fallback to
    ``RESOURCE_SERVER_URL``). Its absence crash-loops the container on a fresh deploy."""
    doc = yaml.safe_load(_COMPOSE.read_text())
    env = doc["services"]["mcp"]["environment"]
    assert "REBAR_MCP_AUTH_ISSUER_URL" in env, (
        "mcp service env omits REBAR_MCP_AUTH_ISSUER_URL; AuthSettings will fail to "
        "construct on a fresh deploy"
    )
    assert str(env["REBAR_MCP_AUTH_ISSUER_URL"]).strip() != ""


def test_compose_mcp_publishes_dedicated_loopback_port_8091() -> None:
    """AC3-adjacent: the service publishes ONLY on host loopback, on the dedicated
    port 8091 (not clashing with gerrit 8080 / opcert 8090)."""
    doc = yaml.safe_load(_COMPOSE.read_text())
    ports = doc["services"]["mcp"]["ports"]
    assert ports == ["127.0.0.1:8091:8091"]


def test_compose_mcp_has_health_check_against_health_endpoint() -> None:
    """AC1: the service healthcheck probes the /health endpoint."""
    doc = yaml.safe_load(_COMPOSE.read_text())
    test = doc["services"]["mcp"]["healthcheck"]["test"]
    assert any("/health" in part for part in test)


def test_compose_mcp_stop_grace_covers_the_module_shutdown_budget() -> None:
    """AC4: stop_grace_period >= DEFAULT_SHUTDOWN_GRACE_SECONDS so Docker never
    SIGKILLs a container mid-drain."""
    doc = yaml.safe_load(_COMPOSE.read_text())
    grace = _duration_to_seconds(doc["services"]["mcp"]["stop_grace_period"])
    assert grace >= DEFAULT_SHUTDOWN_GRACE_SECONDS


def test_compose_mcp_declares_mcp_paths_for_autodeploy_detector() -> None:
    """The autodeploy change-detector paths (foxterrier) are declared as a comment so
    the downstream slice can consume them."""
    raw = _COMPOSE.read_text()
    assert "MCP_PATHS" in raw


# --------------------------------------------------------------------------- Dockerfile


def test_dockerfile_mcp_builds_locked_with_agents_and_mcp_extras() -> None:
    """AC1/AC2: Dockerfile.mcp installs through the committed uv.lock with the agents +
    mcp extras (same recipe as Dockerfile.opcert)."""
    text = _DOCKERFILE.read_text()
    assert "uv sync --locked --no-dev --extra agents --extra mcp" in text
    assert "FROM python:3.12-slim" in text


def test_dockerfile_mcp_has_healthcheck_hitting_health() -> None:
    """AC1: a container HEALTHCHECK hits the MCP /health endpoint."""
    text = _DOCKERFILE.read_text()
    assert "HEALTHCHECK" in text
    hc_line = next(line for line in text.splitlines() if "urlopen" in line)
    assert "/health" in hc_line


def test_dockerfile_mcp_boots_rebar_mcp() -> None:
    """CMD boots rebar-mcp (HTTP via env)."""
    assert 'CMD ["rebar-mcp"]' in _DOCKERFILE.read_text()


# --------------------------------------------------------------------------- nginx wiring


def test_nginx_has_named_mcp_upstream_from_materialized_glob_include() -> None:
    """AC3: a NAMED `upstream rebar_mcp` whose server line is a materialized glob
    include (the atomic-flip seam) — NOT a hardcoded proxy_pass host:port."""
    text = _NGINX.read_text()
    m = re.search(r"upstream\s+rebar_mcp\s*\{(.*?)\}", text, re.DOTALL)
    assert m, "named `upstream rebar_mcp` block not found"
    body = m.group(1)
    assert re.search(r"include\s+/etc/nginx/mcp-upstream\*?\.conf\s*;", body), body


def test_nginx_mcp_location_proxies_to_the_named_upstream() -> None:
    """AC3: a `location /mcp` proxies to the named upstream (proxy_pass
    http://rebar_mcp), preserving the URI (no path on proxy_pass)."""
    text = _NGINX.read_text()
    m = re.search(r"location\s+/mcp\b[^{]*\{(.*?)\}", text, re.DOTALL)
    assert m, "`location /mcp` block not found"
    body = m.group(1)
    assert re.search(r"proxy_pass\s+http://rebar_mcp\s*;", body), body
    # SSE/Streamable-HTTP needs HTTP/1.1 upstream + a cleared Connection header, else
    # nginx talks HTTP/1.0 to the MCP server and streaming responses break.
    assert re.search(r"proxy_http_version\s+1\.1\s*;", body), body
    assert re.search(r'proxy_set_header\s+Connection\s+""\s*;', body), body


def test_committed_seed_supplies_the_default_backend() -> None:
    """AC3: the committed seed supplies exactly the loopback backend 127.0.0.1:8091."""
    assert _SEED.is_file()
    assert re.search(r"server\s+127\.0\.0\.1:8091\s*;", _SEED.read_text())


# --------------------------------------------------------------------------- installer wiring


def test_materialize_script_is_executable_and_installs_only_if_absent() -> None:
    """AC3: the fresh-boot installer exists, is executable, copies the committed seed to
    the nginx include path only-if-absent (never clobbering a flip), and reloads nginx."""
    assert _MATERIALIZE.is_file() and os.access(_MATERIALIZE, os.X_OK)
    text = _MATERIALIZE.read_text()
    assert "mcp-upstream.conf" in text
    # only-if-absent guard: it checks for the target file before copying.
    assert re.search(r"if\s+\[\s+-f\s+\"\$NGINX_UPSTREAM_FILE\"\s+\]", text)
    assert "cp " in text
    assert "nginx -s reload" in text


def test_compose_up_wires_the_mcp_upstream_materialize_call() -> None:
    """AC3: compose-up.sh calls materialize-mcp-upstream.sh (non-fatal WARN) before
    `docker compose up`, beside the opcert-guard call."""
    text = _COMPOSE_UP.read_text()
    assert "materialize-mcp-upstream.sh" in text
    # non-fatal pattern: guarded by `if ! bash ...; then WARN`.
    pattern = r"if\s+!\s+bash\s+\"\$\{REPO_ROOT\}/infra/scripts/materialize-mcp-upstream\.sh\""
    assert re.search(pattern, text)


def test_mcp_bluegreen_refreshes_ssm_secrets_before_starting_the_new_container() -> None:
    """The mcp deploy path must re-materialize its SSM-sourced secrets, like the bot path does.

    `mcp` consumes two artifacts that are rsync-EXCLUDED and SSM-materialized -- the `.env`
    (`mcp_run_new --env-file`) and `mcp-static-tokens.json` (`mcp_run_new -v`). Nothing else in a
    deploy regenerates either, and populating an SSM SecureString advances no git ref, so without
    an explicit `fetch-secrets.sh` call a rotated credential NEVER reaches a new container.

    This is not theoretical. On 2026-08-25 three MCP client PATs were populated in SSM at 16:42Z;
    the on-disk tokens file was still `{"tokens": []}` two minutes later, and every blue-green
    container failed closed at `_mcp_auth.py` ("static tokens file ... defines no tokens") until
    an operator ran `fetch-secrets.sh` by hand. The blue-green retry loop burned nine attempts
    into a 900s backoff and held the `rebar-autodeploy-errors` alarm in ALARM
    (bug receptive-houndy-nilgai). The review-bot path had this call all along.

    Pinned here rather than in the fail-open direction on purpose: the call must sit BEFORE the
    new container is started, so a failure aborts while the OLD container is still serving.
    """
    script = _AUTODEPLOY.read_text(encoding="utf-8")

    # Anchor on the block's own log line, not on the gate EXPRESSION. This test cares about
    # ordering INSIDE the mcp block (fetch-secrets before mcp_run_new); how the gate is spelled
    # is not its business, and pinning the expression made it break when the gate learned to
    # diff from a per-component marker — a change-detector failure, not a real regression.
    marker = "mcp sources changed"
    assert marker in script, (
        "could not locate the mcp blue-green block: its opening log line "
        f"({marker!r}) is missing from autodeploy.sh"
    )
    block = script[script.index(marker) :]

    assert "fetch-secrets.sh" in block, (
        "the mcp blue-green block never calls fetch-secrets.sh, so an SSM secret rotation "
        "cannot reach a new mcp container (receptive-houndy-nilgai)"
    )

    # It must run BEFORE the container is launched, otherwise the new container has already
    # bind-mounted the stale file by the time the secrets are refreshed. Match the INVOCATION,
    # not the bare name: the surrounding comments mention `mcp_run_new` while explaining which
    # artifacts it mounts, and a bare-name search matches that prose instead of the call site.
    invocation = re.search(r"^\s*if\s+!\s+mcp_run_new\b", block, re.MULTILINE)
    assert invocation, "could not locate the mcp_run_new invocation in the blue-green block"
    assert block.index("fetch-secrets.sh") < invocation.start(), (
        "fetch-secrets.sh must run BEFORE mcp_run_new starts the replacement container"
    )

    # Fail-fast, matching the bot path: an SSM error aborts the deploy rather than proceeding
    # with stale secrets.
    assert "mcp-secrets-fetch-failed" in block, (
        "a fetch-secrets failure in the mcp path must abort the deploy with a named error, "
        "so the old container keeps serving instead of being replaced using stale secrets"
    )


# --------------------------------------------------------------------------- ticket store

# The MCP tools read AND WRITE a rebar ticket store, but `.dockerignore` excludes
# `.tickets-tracker` (and `.git`), so one can never be baked into the image. Without an
# explicitly configured tracker dir the path resolved to the WORKDIR and every tool failed
# "/app/.tickets-tracker not found". These pin the provisioning wiring that fixes it:
# a persistent external volume, the tracker-dir env pointing at it, and an entrypoint that
# clones the `tickets` branch there and converges it into a WRITABLE store.
#
# Deliberately NOT pinned: any "fails when the PAT is missing" behaviour. The posture is
# SOFT by design — no PAT (or a failed clone) defers provisioning and the container still
# boots, exactly like the review-bot's deploy canary.

_MCP_TICKETS_DIR = "/var/gerrit/site/mcp-tickets"
_MCP_TICKETS_VOLUME = "gerrit_mcp_tickets"


def test_compose_mcp_mounts_the_tracker_volume_and_points_rebar_at_it() -> None:
    """The `mcp` service mounts the persistent ticket store and sets REBAR_TRACKER_DIR to
    that same in-container path, so the tools do not fall back to the WORKDIR."""
    doc = yaml.safe_load(_COMPOSE.read_text())
    svc = doc["services"]["mcp"]
    assert svc["environment"]["REBAR_TRACKER_DIR"] == _MCP_TICKETS_DIR
    assert f"{_MCP_TICKETS_VOLUME}:{_MCP_TICKETS_DIR}" in svc["volumes"]


def test_compose_mcp_passes_the_tickets_pat_through() -> None:
    """The clone credential reaches the container as MCP_TICKETS_PAT (blank is fine)."""
    doc = yaml.safe_load(_COMPOSE.read_text())
    assert "MCP_TICKETS_PAT" in doc["services"]["mcp"]["environment"]


def test_compose_declares_the_mcp_tickets_volume_external() -> None:
    """external:true so `docker compose down -v` cannot destroy accumulated ticket events."""
    doc = yaml.safe_load(_COMPOSE.read_text())
    assert doc["volumes"][_MCP_TICKETS_VOLUME]["external"] is True


def test_compose_up_creates_the_mcp_tickets_site_subdir() -> None:
    """SITE_SUBDIRS is the single source of truth for the external volumes; config-check.sh
    check 5 diffs it against the compose file, so a compose volume without this entry fails
    CI (the incident-2731 drift class)."""
    text = _COMPOSE_UP.read_text()
    m = re.search(r'^SITE_SUBDIRS="([^"]*)"', text, re.MULTILINE)
    assert m, "SITE_SUBDIRS assignment not found"
    assert "mcp-tickets" in m.group(1).split()


def test_dockerfile_mcp_entrypoint_provisions_the_ticket_store() -> None:
    """The image ships an ENTRYPOINT that clones the `tickets` branch into the tracker dir
    and converges it into a writable store via the SHARED review-bot ensure script."""
    text = _DOCKERFILE.read_text()
    entrypoint = _ENTRYPOINT.read_text(encoding="utf-8")
    assert re.search(r"^ENTRYPOINT\s+\[", text, re.MULTILINE), "no ENTRYPOINT declared"
    assert f"REBAR_TRACKER_DIR={_MCP_TICKETS_DIR}" in text
    assert "git clone --single-branch --branch tickets" in entrypoint
    # Reused, not forked: the review-bot's converge script, invoked with the target dir.
    assert "reviewbot-ensure-tickets.sh" in entrypoint
    # ...and the server is still the container's command.
    assert 'CMD ["rebar-mcp"]' in text


def test_autodeploy_mcp_run_new_mounts_the_same_ticket_store() -> None:
    """Blue-green containers bypass compose (`docker run`), so the volume + tracker dir must
    be spelled out there too or a deployed container serves a store that does not exist."""
    script = _AUTODEPLOY.read_text(encoding="utf-8")
    start = script.index("mcp_run_new() {")
    body = script[start : script.index("\n}", start)]
    assert f"REBAR_TRACKER_DIR={_MCP_TICKETS_DIR}" in body
    assert f"{_MCP_TICKETS_VOLUME}:{_MCP_TICKETS_DIR}" in body


def test_fetch_secrets_materializes_the_mcp_tickets_pat() -> None:
    """The PAT is read from the SSM leaf via the OPTIONAL helper (blank ⇒ clone deferred)
    and emitted into the generated .env the mcp container reads."""
    text = _FETCH_SECRETS.read_text()
    assert "get_param_optional mcp-tickets-pat" in text
    assert "MCP_TICKETS_PAT=${mcp_tickets_pat}" in text


def test_mcp_tickets_pat_falls_back_to_the_reviewbot_credential() -> None:
    """An empty dedicated slot must not leave the mcp store unprovisioned.

    Both credentials do the same thing against the same target -- clone and push the
    `tickets` branch of the same repo. Without this fallback the endpoint keeps reporting an
    uninitialized store purely for want of a second copy of an equivalent secret already
    sitting in SSM.

    Two properties are pinned, and the ORDER matters: the dedicated slot is read first so it
    still wins when set (an operator can scope mcp separately later with no code change), and
    the fallback must be read AFTER `reviewbot_tickets_pat` is assigned, or it would silently
    substitute an empty string and look like it worked.
    """
    script = _FETCH_SECRETS.read_text(encoding="utf-8")

    assert 'mcp_tickets_pat="$(get_param_optional mcp-tickets-pat)"' in script, (
        "the dedicated slot must remain the preferred source"
    )
    assert '[ -z "${mcp_tickets_pat}" ] && [ -n "${reviewbot_tickets_pat}" ]' in script, (
        "an empty dedicated slot must fall back to the review-bot tickets PAT"
    )
    assert script.index('reviewbot_tickets_pat="$(get_param_optional') < script.index(
        '[ -z "${mcp_tickets_pat}" ]'
    ), "the fallback must come AFTER reviewbot_tickets_pat is read, or it substitutes empty"


def test_static_tokens_have_no_such_fallback() -> None:
    """The auth boundary stays fail-closed -- the store fallback must not spread to it.

    A missing bearer store must never result in serving an unauthenticated endpoint; a
    missing ticket clone is a soft degrade. Different risks, deliberately different postures.
    This pins that the convenience added for the latter was not copied to the former.
    """
    script = _FETCH_SECRETS.read_text(encoding="utf-8")

    for client in ("copilot", "codex", "claude"):
        assert f'[ -z "${{mcp_pat_{client}}}" ] &&' not in script, (
            f"mcp-client-pat-{client} must NOT gain a fallback; the static-token file is an "
            "auth boundary and fails closed by design"
        )


def _require(haystack: str, needle: str, what: str) -> int:
    """Index of `needle`, with a MESSAGE when it is absent.

    `str.index` raises a bare ``ValueError: substring not found`` that names neither the
    thing looked for nor why it mattered, so an ordering assertion built on it fails
    uninformatively the moment the text is refactored.
    """
    at = haystack.find(needle)
    assert at != -1, f"{what}: expected to find {needle!r}"
    return at


def test_mcp_entrypoint_is_a_real_file_installed_onto_path() -> None:
    """The entrypoint is a FILE, not a heredoc echoed into existence inside a RUN block.

    It used to be generated by a `RUN { echo '...'; ... } > /usr/local/bin/...` block, which
    meant nothing could ever EXECUTE it outside a built container: the only assertions
    available were greps over this Dockerfile's source, i.e. change detectors that pass
    whether or not the script works. As a real file it is installed exactly the way
    `Dockerfile.reviewbot` installs `reviewbot-ensure-tickets.sh`, and its behaviour is
    driven directly by tests/unit/test_mcp_entrypoint_provisioning.py.
    """
    text = _DOCKERFILE.read_text(encoding="utf-8")

    assert _ENTRYPOINT.is_file(), f"the entrypoint script must exist at {_ENTRYPOINT}"
    assert os.access(_ENTRYPOINT, os.X_OK), "the entrypoint script must be executable"
    assert "install -m 0755 /app/infra/scripts/mcp-entrypoint.sh" in text, (
        "the entrypoint must be INSTALLED from the repo file (COPY . /app puts it at "
        "/app/infra/scripts), not regenerated inside a RUN block"
    )
    assert 'ENTRYPOINT ["/usr/local/bin/mcp-entrypoint.sh"]' in text, (
        "the installed script must be the image ENTRYPOINT"
    )
    assert "echo '#!/bin/sh'" not in text, (
        "the entrypoint must no longer be echoed into existence — that shape is what made it "
        "untestable in the first place"
    )
    # The source must be reachable from the build context (context = repo root).
    assert not any(
        line.strip() in {"infra", "infra/scripts", "infra/scripts/mcp-entrypoint.sh"}
        for line in _DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    ), ".dockerignore must not exclude the entrypoint script from the build context"


def test_mcp_entrypoint_does_not_block_boot_on_the_tickets_clone() -> None:
    """The store clone must NOT run before `exec` — it cannot finish inside the health gate.

    The `tickets` branch is ~200k commits. The blue-green readiness deadline is
    `MCP_HEALTH_TIMEOUT` (120s, `infra/scripts/autodeploy.sh`). Cloning before `exec` meant the
    server never bound a port, `/health` never answered, and EVERY deploy was rolled back with
    `mcp-unhealthy` — the endpoint kept serving stale code while `main` moved on
    (bug unfit-beneficial-whimbrel).

    The pattern was copied from `Dockerfile.reviewbot`, which clones at entrypoint too. That is
    safe THERE because the review-bot has no blue-green readiness gate. The lesson this test
    pins is that a blocking entrypoint step and a readiness deadline cannot coexist.

    Structural by necessity: whether a step blocks PID 1 is a property of the script's
    control flow, not of any observable it could return. The BEHAVIOUR of provisioning is
    executed in tests/unit/test_mcp_entrypoint_provisioning.py.
    """
    body = _ENTRYPOINT.read_text(encoding="utf-8")

    background_at = _require(
        body,
        "provision_store &",
        "store provisioning must be BACKGROUNDED so the server execs immediately; a "
        "synchronous clone cannot complete inside the 120s blue-green health deadline",
    )
    clone_at = _require(
        body,
        "git clone --single-branch --branch tickets",
        "the entrypoint must still clone the tickets branch",
    )
    exec_at = _require(body, 'exec "$@"', "the entrypoint must exec the command")
    assert clone_at < background_at < exec_at, (
        "the clone must be defined inside provision_store (which is then backgrounded) and "
        "`exec` must follow the backgrounding, so boot never waits on the clone"
    )


def test_mcp_entrypoint_still_execs_the_server_last() -> None:
    """Backgrounding must not have cost us PID-1 semantics.

    `exec "$@"` must remain the final action so the server replaces the shell and receives
    SIGTERM directly — the container's stop_grace_period and the in-flight drain depend on it.
    """
    lines = [ln.strip() for ln in _ENTRYPOINT.read_text(encoding="utf-8").splitlines()]
    executable = [ln for ln in lines if ln and not ln.startswith("#")]
    assert executable, "entrypoint must not be empty"
    assert executable[-1] == 'exec "$@"', (
        f"exec must be the LAST action in the entrypoint; found {executable[-1]!r}"
    )


# --------------------------------------------------------------------------- Jira bridge creds
#
# The Jira wiring the bridge tools need (bug colourless-hasteless-lamb). Without it every live
# bridge operation on the deployed server failed at once: `bridge_check_access` exited 2 with
# "bridge access check requires JIRA_URL, JIRA_USER, and JIRA_API_TOKEN".
#
# Two properties are pinned throughout, and they are the two a future edit is most likely to
# quietly break:
#
#   1. The var/secret SPLIT. GitHub Actions models JIRA_URL/JIRA_USER/JIRA_PROJECT as non-secret
#      `vars.*` and only JIRA_API_TOKEN as a `secrets.*`; rebar's own _child_env.py registry
#      agrees. The box preserves that -- three Strings, one SecureString -- so that reading or
#      correcting a wrong Jira URL never requires decrypting anything and rotating the token
#      never disturbs the rest. "Tidying" all four into SecureString is the regression.
#
#   2. The SOFT-degrade posture. Absent Jira credentials must leave the endpoint UP. They are an
#      outbound integration credential, not an auth boundary, so unlike mcp-static-tokens.json
#      (which fails CLOSED -- no bearer store, no endpoint) a blank slot must never abort
#      fetch-secrets.sh, which would write no .env at all and take every container down.

_JIRA_PLAIN_LEAVES = ("jira-url", "jira-user", "jira-project")
_JIRA_ENV = ("JIRA_URL", "JIRA_USER", "JIRA_PROJECT", "JIRA_API_TOKEN")


def test_ssm_declares_the_jira_token_as_the_only_securestring_leaf() -> None:
    """Only the API token is a secret; the other three must NOT be in the SecureString list."""
    text = _SSM_TF.read_text(encoding="utf-8")
    secret_block = re.search(r"rebar_secret_params\s*=\s*\[(.*?)^  \]", text, re.DOTALL | re.M)
    assert secret_block, "rebar_secret_params list not found"
    body = secret_block.group(1)
    assert '"/rebar/prod/jira-api-token"' in body
    for leaf in _JIRA_PLAIN_LEAVES:
        assert f'"/rebar/prod/{leaf}"' not in body, (
            f"{leaf} is a NON-secret (a `vars.*` in GitHub Actions) and must not be a "
            "SecureString -- that costs rotation and debuggability for no security gain"
        )


def test_ssm_declares_the_non_secret_jira_leaves_as_plain_strings() -> None:
    """url/user/project are `String`, and the token is never declared as one."""
    text = _SSM_TF.read_text(encoding="utf-8")
    for leaf in _JIRA_PLAIN_LEAVES:
        assert f'"/rebar/prod/{leaf}"' in text, f"{leaf} must be declared"
    for resource in ("rebar_plain_seeded", "jira_project"):
        block = re.search(
            rf'resource "aws_ssm_parameter" "{resource}" \{{(.*?)^\}}', text, re.DOTALL | re.M
        )
        assert block, f"resource {resource} not found"
        assert 'type  = "String"' in block.group(1), f"{resource} must be a plain String"
        assert "jira-api-token" not in block.group(1), (
            "the API token must never be declared as a plaintext String"
        )


def test_ssm_adopts_the_operator_seeded_jira_parameters_instead_of_recreating_them() -> None:
    """url/user/token were created OUT OF BAND by an operator, so terraform must ADOPT them.

    Without an import block the next `apply` fails `ParameterAlreadyExists` -- the parameters
    exist in SSM but not in state. Only these three: jira-project is terraform-created.
    """
    text = _SSM_TF.read_text(encoding="utf-8")
    for leaf in ("jira-url", "jira-user", "jira-api-token"):
        assert re.search(rf'import \{{[^}}]*?id = "/rebar/prod/{leaf}"', text, re.DOTALL), (
            f"/rebar/prod/{leaf} was operator-seeded out of band and needs an import block"
        )
    assert not re.search(r'import \{[^}]*?id = "/rebar/prod/jira-project"', text, re.DOTALL), (
        "jira-project is fully terraform-managed and is CREATED by apply, not imported"
    )


def test_ssm_holds_no_jira_credential_value() -> None:
    """Terraform owns each parameter's existence and type -- never an operator's value."""
    text = _SSM_TF.read_text(encoding="utf-8")
    seeded = re.search(
        r'resource "aws_ssm_parameter" "rebar_plain_seeded" \{(.*?)^\}', text, re.DOTALL | re.M
    )
    assert seeded and 'value = "CHANGEME"' in seeded.group(1)
    assert "ignore_changes = [value]" in seeded.group(1), (
        "an operator-seeded value must never be reverted to the placeholder by a later apply"
    )


def test_ssm_jira_project_matches_the_configured_project_key() -> None:
    """The project key lives in two places by necessity; pin them equal so they cannot drift.

    `rebar.config.resolve_jira_probe_scope` reads the ENVIRONMENT ONLY, so `rebar.toml`'s
    `[jira] project` never reaches `access_check._resolve_probe_scope` -- which fails closed with
    `missing_project`. The box therefore needs its own copy, and this is the guard that keeps the
    copy honest.
    """
    tf = _SSM_TF.read_text(encoding="utf-8")
    block = re.search(
        r'resource "aws_ssm_parameter" "jira_project" \{(.*?)^\}', tf, re.DOTALL | re.M
    )
    assert block, "jira_project resource not found"
    declared = re.search(r'value = "([^"]+)"', block.group(1))
    assert declared, "jira_project must carry a real terraform-managed value"
    configured = re.search(
        r"^project\s*=\s*\"([^\"]+)\"", _REBAR_TOML.read_text(encoding="utf-8"), re.M
    )
    assert configured, "rebar.toml [jira] project not found"
    assert declared.group(1) == configured.group(1), (
        f"ssm.tf jira-project={declared.group(1)!r} has drifted from rebar.toml "
        f"[jira] project={configured.group(1)!r}"
    )


def test_fetch_secrets_reads_each_jira_leaf_at_its_declared_type() -> None:
    """The SecureString is decrypted; the three Strings are read WITHOUT --with-decryption."""
    text = _FETCH_SECRETS.read_text(encoding="utf-8")
    assert 'jira_api_token="$(get_param_optional jira-api-token)"' in text
    for var, leaf in (
        ("jira_url", "jira-url"),
        ("jira_user", "jira-user"),
        ("jira_project", "jira-project"),
    ):
        assert f'{var}="$(get_param_optional_plain {leaf})"' in text, (
            f"{leaf} is a plain String and must be read by the non-decrypting reader"
        )
    start = text.index("get_param_optional_plain() {")
    body = text[start : text.index("\n}", start)]
    assert "--with-decryption" not in body, (
        "the plain reader must omit --with-decryption; blanket-decrypting erases the very "
        "secret/non-secret distinction this wiring preserves"
    )


def test_fetch_secrets_emits_every_jira_variable_into_the_env() -> None:
    """All four land in the generated .env -- the single carrier into the mcp container."""
    text = _FETCH_SECRETS.read_text(encoding="utf-8")
    for env_var, shell_var in zip(
        _JIRA_ENV, ("jira_url", "jira_user", "jira_project", "jira_api_token"), strict=True
    ):
        assert f"{env_var}=${{{shell_var}}}" in text


def test_absent_jira_credentials_never_abort_the_secrets_fetch() -> None:
    """SOFT degrade, pinned at the mechanism that decides it.

    `get_param` exits 1 on an empty/None/CHANGEME value and writes NO .env at all, and
    autodeploy aborts the entire mcp deploy when fetch-secrets fails. So a REQUIRED read of a
    Jira leaf would let one unpopulated slot block deploys of unrelated code and take every
    container down -- the opposite of "the endpoint stays up". Only the optional readers are
    permitted here. Contrast test_static_tokens_have_no_such_fallback above: that boundary
    fails CLOSED on purpose, and the two postures must not be conflated.
    """
    text = _FETCH_SECRETS.read_text(encoding="utf-8")
    for leaf in (*_JIRA_PLAIN_LEAVES, "jira-api-token"):
        assert f"get_param {leaf}" not in text, (
            f"{leaf} must use an OPTIONAL reader; the fail-fast get_param would abort the whole "
            "boot for a missing outbound integration credential"
        )


def test_compose_mcp_declares_every_jira_variable() -> None:
    """The mcp service documents all four inputs rather than leaving them implicit in .env."""
    doc = yaml.safe_load(_COMPOSE.read_text())
    env = doc["services"]["mcp"]["environment"]
    for key in _JIRA_ENV:
        assert key in env, f"the mcp service must declare {key}"


def test_autodeploy_carries_the_jira_variables_by_env_file_not_by_dash_e() -> None:
    """Blue-green parity, and the one way to get it WRONG.

    `docker run` does not interpolate the project `.env`, so `-e JIRA_URL=${JIRA_URL:-}` would
    expand in the deploy shell -- where it is unset -- and pass an empty value that OVERRIDES
    the real one from `--env-file`. The container would then look correctly configured and
    still fail every bridge call. `--env-file` is the carrier, exactly as for MCP_TICKETS_PAT.
    """
    script = _AUTODEPLOY.read_text(encoding="utf-8")
    start = script.index("mcp_run_new() {")
    body = script[start : script.index("\n}", start)]
    assert "--env-file" in body, "the env-file carrier must remain"
    for key in _JIRA_ENV:
        assert f"-e {key}=" not in body and f'-e "{key}=' not in body, (
            f"{key} must reach the container via --env-file; an `-e` with an un-interpolated "
            "compose default would blank the real value"
        )
