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
