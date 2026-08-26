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

    start = script.index('if changed "$MCP_PATHS"; then')
    block = script[start:]

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
    assert re.search(r"^ENTRYPOINT\s+\[", text, re.MULTILINE), "no ENTRYPOINT declared"
    assert f"REBAR_TRACKER_DIR={_MCP_TICKETS_DIR}" in text
    assert "git clone --single-branch --branch tickets" in text
    # Reused, not forked: the review-bot's converge script, invoked with the target dir.
    assert "reviewbot-ensure-tickets.sh" in text
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
