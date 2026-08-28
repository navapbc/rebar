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
    http://rebar_mcp), preserving the URI (no path on proxy_pass).

    The selector anchors on a LINE-LEADING directive (and closes on a line-leading
    `}`) so it cannot latch onto prose. The previous `location\\s+/mcp\\b[^{]*\\{`
    matched the words "location /mcp" inside a COMMENT and then ran forward to the
    next `{`, capturing whichever block happened to follow. Assertions unchanged."""
    text = _NGINX.read_text()
    m = re.search(r"^[ \t]*location\s+/mcp\s*\{(.*?)^[ \t]*\}", text, re.DOTALL | re.MULTILINE)
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


# ------------------------------------------------------- configured provider <-> image extras

_REBAR_TOML = _REPO / "rebar.toml"

# Which `pyproject.toml` optional-dependency group supplies the client package for a given
# provider qualifier. `anthropic` rides inside `agents`
# (`pydantic-ai-slim[anthropic,...]`); `bedrock` is a deliberately SEPARATE opt-in extra so
# boto3 stays out of the default install (ticket sporadic-bratty-porcupine). A provider with
# no entry here has no extra that can serve it, which the test reports as such rather than
# silently skipping.
_EXTRA_SUPPLYING_PROVIDER: dict[str, str] = {
    "anthropic": "agents",
    "bedrock": "bedrock",
}


def _configured_llm_providers() -> set[str]:
    """Every provider the project's own `rebar.toml` can select for an LLM op.

    Covers the scalar `[llm] model` (a SECOND resolution path the class table cannot
    reach — see the rationale block above `[llm]` in rebar.toml), every
    `[llm.model_classes]` primary, and every `fallback` entry, because
    `model_classes.should_fall_back` can route a live call to a fallback provider.

    Provider inference goes through rebar's OWN `infer_provider` rather than a local regex,
    so this test cannot drift away from the resolver it is guarding.
    """
    import tomllib

    from rebar.llm.config import infer_provider

    llm = tomllib.loads(_REBAR_TOML.read_text(encoding="utf-8")).get("llm", {})
    models: list[str] = []
    if isinstance(llm.get("model"), str):
        models.append(llm["model"])
    for slot in (llm.get("model_classes") or {}).values():
        if not isinstance(slot, dict):
            continue
        if isinstance(slot.get("model"), str):
            models.append(slot["model"])
        for entry in slot.get("fallback") or []:
            if isinstance(entry, dict) and isinstance(entry.get("model"), str):
                models.append(entry["model"])

    providers = {p for p in (infer_provider(m, None) for m in models) if p}
    assert providers, (
        "parsed no LLM providers out of rebar.toml — the parse is broken, or the [llm] "
        "table lost its model config; either way this guard would pass vacuously"
    )
    return providers


def _dockerfile_mcp_installed_extras() -> set[str]:
    """The extras Dockerfile.mcp's locked `uv sync` actually installs."""
    line = next(
        (
            ln
            for ln in _DOCKERFILE.read_text(encoding="utf-8").splitlines()
            if "uv sync" in ln and not ln.lstrip().startswith("#")
        ),
        None,
    )
    assert line is not None, "Dockerfile.mcp must install its dependencies with `uv sync`"
    return set(re.findall(r"--extra[= ]([A-Za-z0-9_-]+)", line))


def test_dockerfile_mcp_installs_an_extra_for_every_configured_llm_provider() -> None:
    """The image must be able to SERVE every provider the config can SELECT.

    This is the defect behind bug d43e-c24f-dd39-44e9: `rebar.toml` pins
    `bedrock:us.anthropic.claude-opus-4-8`, but the image installed only `agents` + `mcp`.
    `[agents]` is `pydantic-ai-slim[anthropic,retries,duckduckgo]` — it has never carried
    Bedrock — and `--no-dev` excludes the `[dev]` group that masks this locally. So the
    container started clean and every certified LLM tool failed at gate time with
    "the optional bedrock provider package is not installed", returning an unsigned
    INDETERMINATE and minting no plan-review attestation.

    Asserted as a COUPLING between two files, not as a fixed string: re-point the config at
    a provider the image cannot serve and this fails, which is the class of defect rather
    than just this instance of it.
    """
    configured = _configured_llm_providers()
    installed = _dockerfile_mcp_installed_extras()

    unserviceable = sorted(p for p in configured if p not in _EXTRA_SUPPLYING_PROVIDER)
    assert not unserviceable, (
        f"rebar.toml configures provider(s) {unserviceable} that no pyproject extra supplies; "
        "add the extra (and map it in _EXTRA_SUPPLYING_PROVIDER) before the image can serve it"
    )

    missing = sorted(
        {_EXTRA_SUPPLYING_PROVIDER[p] for p in configured} - installed,
    )
    assert not missing, (
        f"Dockerfile.mcp installs extras {sorted(installed)}, which cannot serve every "
        f"provider configured in rebar.toml ({sorted(configured)}): missing extra(s) "
        f"{missing}. Add them to the `uv sync` line, or the deployed MCP server degrades to "
        "the deterministic floor and mints no op-cert."
    )


# --------------------------------------------------------------- mcp Bedrock region wiring

# The instance runs in us-east-1 and rebar.toml pins the same region beside the region-scoped
# `us.*` inference-profile ids.
_MCP_REGION_VARS = ("REBAR_LLM_BEDROCK_REGION", "AWS_DEFAULT_REGION")


def test_compose_mcp_sets_both_bedrock_region_vars() -> None:
    """The mcp service must resolve an AWS region, exactly as the review-bot service does.

    MEASURED on the box for the sibling service (docker-compose.yml's review-bot env, ticket
    a574): with neither var set, in-container `boto3.session.Session().region_name` is None
    and bedrock-runtime construction raises NoRegionError — independently of IMDS, which
    resolves fine (ticket 9249). BOTH vars are required and they are not interchangeable:
    rebar's own knob threads into BedrockProvider and is recorded in the signed verdict's
    provider provenance, while AWS_DEFAULT_REGION is what any bare boto3 caller reads.
    """
    svc = yaml.safe_load(_COMPOSE.read_text())["services"]["mcp"]
    env = svc["environment"]
    for var in _MCP_REGION_VARS:
        assert env.get(var), (
            f"the mcp service must set {var}; without a resolvable region the Bedrock "
            "provider fails closed at gate time even with valid instance-role credentials"
        )
    assert env["REBAR_LLM_BEDROCK_REGION"] == env["AWS_DEFAULT_REGION"], (
        "the two region vars must agree; a split would let rebar's provider and a bare boto3 "
        "caller talk to different regions"
    )


def _mcp_run_new_body() -> str:
    """The body of autodeploy.sh's `mcp_run_new`, so a match in a sibling function can't count."""
    script = _AUTODEPLOY.read_text(encoding="utf-8")
    start = script.index("mcp_run_new() {")
    end = script.index("\n}", start)
    return script[start:end]


def test_autodeploy_mcp_run_new_passes_the_bedrock_region() -> None:
    """The blue-green `docker run` must stay at parity with the compose service.

    `mcp_run_new` — not compose — is the path an actual autodeploy takes, so region vars
    present only in docker-compose.yml would never reach a deployed container.
    """
    body = _mcp_run_new_body()
    for var in _MCP_REGION_VARS:
        assert f"-e {var}=" in body or f'-e "{var}=' in body, (
            f"mcp_run_new must pass {var}; docker-compose.yml declares it for the same "
            "service and autodeploy is what actually starts the container"
        )


# ------------------------------------------------------- MCP edge: no scheme downgrade
# Regression guard for fernlike-toothsome-hen (79b2-6ebc-6c1d-4125), a P1 security bug:
# the documented client URL `https://<box>/mcp/` answered `307` with
# `location: http://<box>/mcp` — an HTTPS→HTTP downgrade on the URL every doc and every
# committed example points at. A 307 preserves method and body, and clients differ on
# whether they keep `Authorization` across a same-host scheme change, so a client that
# keeps it puts the bearer PAT on the wire in plaintext.
#
# Two mechanisms produced it, and both are pinned below:
#   (a) the edge was a PREFIX `location /mcp` with a URI-preserving proxy_pass, so the
#       external `/mcp/` reached the app verbatim while the app mounts at http_path
#       `/mcp`; Starlette's default `redirect_slashes` then emitted the 307.
#   (b) the app never trusted nginx's `X-Forwarded-Proto: https`, so it built the
#       Location with scheme `http` (see the compose FORWARDED_ALLOW_IPS test).


def _server_443_block() -> str:
    """The body of the port-443 TLS server block."""
    text = _NGINX.read_text()
    start = text.index("listen 443 ssl;")
    return text[start:]


def test_nginx_serves_the_documented_mcp_slash_url_without_redirecting() -> None:
    """The documented external URL `/mcp/` is served DIRECTLY by an exact-match location
    whose proxy_pass carries the app's `/mcp` path, so nginx rewrites the URI at the edge
    and the app's `redirect_slashes` never fires. No Location header is produced at all,
    which is the only way a redirect cannot be downgraded.

    Oracle: an exact-match `location = /mcp/` (and the bare `= /mcp` twin) whose
    proxy_pass names the upstream AND the app path. A PREFIX location alone is exactly
    the defect."""
    text = _NGINX.read_text()
    for external in ("/mcp/", "/mcp"):
        pattern = r"location\s+=\s+" + re.escape(external) + r"\s*\{(.*?)\n    \}"
        m = re.search(pattern, text, re.DOTALL)
        assert m, f"exact-match `location = {external}` block not found"
        body = m.group(1)
        # The app path must be ON the proxy_pass: that is what makes nginx replace the
        # matched URI, so the app is never asked to redirect `/mcp/` to `/mcp`.
        assert re.search(r"proxy_pass\s+http://rebar_mcp/mcp\s*;", body), (
            f"`location = {external}` must proxy_pass to http://rebar_mcp/mcp so the "
            f"edge rewrites the URI instead of letting the app 307-redirect: {body}"
        )


def test_nginx_upgrades_any_plaintext_location_from_the_mcp_upstream() -> None:
    """Fail-closed backstop: even if the MCP upstream ever emits an `http://` Location
    again (an SDK change, a new route, a future redirect), nginx rewrites it to
    `https://` before it reaches the client. Without this directive nothing at the edge
    prevents a scheme downgrade.

    Scoped to the MCP locations deliberately. `proxy_redirect` defaults to `default`,
    which derives its rewrite from that location's own proxy_pass; declaring explicit
    rules at SERVER level would replace that default for `/review/` and `/opcert/` too
    and could break their relative redirects. The MCP blocks proxy to a named upstream
    and rely on no such default, so overriding it there is safe.

    Oracle: every MCP location carries rules mapping an http:// Location — whether the
    app built it from the forwarded Host or from the upstream name — to https://."""
    text = _NGINX.read_text()
    for header, pattern in (
        ("exact `= /mcp/`", r"location\s+=\s+/mcp/\s*\{(.*?)\n    \}"),
        ("exact `= /mcp`", r"location\s+=\s+/mcp\s*\{(.*?)\n    \}"),
        ("prefix `/mcp`", r"location\s+/mcp\s*\{(.*?)\n    \}"),
    ):
        m = re.search(pattern, text, re.DOTALL)
        assert m, f"{header} block not found"
        body = m.group(1)
        assert re.search(r"proxy_redirect\s+http://\$host/\s+https://\$host/\s*;", body), (
            f"{header} lacks `proxy_redirect http://$host/ https://$host/;`: {body}"
        )
        assert re.search(r"proxy_redirect\s+http://rebar_mcp/\s+https://\$host/\s*;", body), (
            f"{header} lacks the named-upstream proxy_redirect rule: {body}"
        )


# --- RFC 9728 protected-resource metadata routing (bug 71fe-2579-28cc-409c) ---
#
# The MCP app's 401 challenge advertises
# `resource_metadata="https://<host>/.well-known/oauth-protected-resource/mcp"`, and the app
# serves that document (probed on the box: app-direct 200, edge 404). The path is a SIBLING
# of /mcp, so no /mcp location matched it and it fell through to `location /` -> Gerrit.

# The one advertised URL, exactly as it appears in the 401 `WWW-Authenticate` header.
_ADVERTISED_METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"


def _render_nginx(review_bot_port: str = "8081") -> str:
    """The template as DEPLOYED, not as committed.

    The deploy renders it with `envsubst '${REVIEW_BOT_PORT}'` (rebar.conf.template:5-12):
    a single-variable substitution whose whole point is that nginx's own `$host`,
    `$remote_addr` etc. survive untouched. Reproducing that one substitution in-process keeps
    the test hermetic (no gettext binary in CI) while asserting on the bytes nginx actually
    parses — a template-only assertion could pass on text that renders to something else.
    """
    return _NGINX.read_text().replace("${REVIEW_BOT_PORT}", review_bot_port)


def test_nginx_routes_the_advertised_resource_metadata_url_to_the_mcp_app() -> None:
    """The advertised `resource_metadata` URL must resolve at the edge, not 404.

    Oracle: the RENDERED config carries an EXACT-match location for the advertised path
    whose proxy_pass names the MCP upstream with NO URI path, so the URI reaches the app
    verbatim at the path it already answers with 200. Exact match (not a prefix) is
    load-bearing: it covers this one document and cannot widen /.well-known/.

    Deleting the location block turns this RED — nothing else in the config matches the
    path, which is precisely the defect."""
    text = _render_nginx()
    pattern = (
        r"^[ \t]*location\s+=\s+" + re.escape(_ADVERTISED_METADATA_PATH) + r"\s*\{(.*?)^[ \t]*\}"
    )
    m = re.search(pattern, text, re.DOTALL | re.MULTILINE)
    assert m, (
        f"no `location = {_ADVERTISED_METADATA_PATH}` block: the URL the 401 advertises "
        f"falls through to `location /` (Gerrit) and 404s"
    )
    body = m.group(1)
    # Named upstream, so a blue-green flip re-points it atomically; NO URI path, so the
    # advertised URI is preserved rather than rewritten.
    assert re.search(r"proxy_pass\s+http://rebar_mcp\s*;", body), (
        f"must proxy_pass to the named upstream with no URI path: {body}"
    )
    assert re.search(r"proxy_set_header\s+X-Forwarded-Proto\s+https\s*;", body), body


def test_nginx_metadata_route_does_not_shadow_the_acme_challenge() -> None:
    """The new /.well-known/ route must not disturb certbot's HTTP-01 webroot.

    An exact-match location cannot shadow the ACME prefix location, but a careless widening
    to `location /.well-known/` would — and certbot renewal failing is a silent TLS outage.
    Oracle: the rendered config still serves the challenge from the certbot webroot."""
    text = _render_nginx()
    m = re.search(
        r"^[ \t]*location\s+/\.well-known/acme-challenge/\s*\{(.*?)^[ \t]*\}",
        text,
        re.DOTALL | re.MULTILINE,
    )
    assert m, "the ACME challenge location disappeared"
    assert re.search(r"root\s+/var/www/certbot\s*;", m.group(1)), m.group(1)


def test_nginx_template_substitutes_cleanly_under_envsubst() -> None:
    """`envsubst '${REVIEW_BOT_PORT}'` substitutes that ONE variable and leaves every other
    `${...}` in place — so any other `${...}` sequence would survive into the deployed
    config verbatim and nginx would reject it. Oracle: the rendered text has none."""
    leftovers = re.findall(r"\$\{[^}]*\}", _render_nginx())
    assert leftovers == [], f"unsubstituted ${{...}} in the rendered config: {leftovers}"


def test_nginx_asserts_hsts_on_every_tls_response() -> None:
    """AC2: an HSTS header is served, so a client cannot be walked down to plaintext.

    `always` is load-bearing: without it nginx omits add_header on error responses, and
    the MCP endpoint's reply to an unauthenticated client is a 401 — precisely the
    response a client sees before it has ever completed a request."""
    body = _server_443_block()
    m = re.search(r'add_header\s+Strict-Transport-Security\s+"([^"]+)"\s+always\s*;', body)
    assert m, 'no `add_header Strict-Transport-Security "..." always;` on the 443 server'
    value = m.group(1)
    max_age = re.search(r"max-age=(\d+)", value)
    assert max_age, f"HSTS header has no max-age: {value!r}"
    # A max-age under a year is not a meaningful transport backstop.
    assert int(max_age.group(1)) >= 31536000, f"HSTS max-age too short: {value!r}"


def test_compose_mcp_trusts_the_edge_forwarded_proto() -> None:
    """Root cause (b) of fernlike-toothsome-hen: the app built absolute URLs with scheme
    `http` even though nginx sends `X-Forwarded-Proto: https`, because uvicorn only
    honours forwarded headers from a TRUSTED peer. uvicorn resolves that trust list from
    the environment — `forwarded_allow_ips=None` -> `os.environ.get("FORWARDED_ALLOW_IPS",
    "127.0.0.1")` — and wraps the app in ProxyHeadersMiddleware with it. This service runs
    INSIDE a container binding 0.0.0.0 with the port published on host loopback, so
    nginx's connection arrives from the docker bridge gateway, NOT 127.0.0.1: the default
    distrusts it and the forwarded scheme is dropped.

    Setting it here needs no code change and corrects EVERY app-generated absolute URL,
    not just the one Location that was reported.

    Spoofing is not a new risk: nginx sets `proxy_set_header X-Forwarded-Proto https;` as
    a LITERAL, so a client-supplied value is always overwritten before the app sees it,
    and the container port is published on host loopback with nginx as the sole reachable
    client — the posture REBAR_MCP_HTTP_TLS_AT_EDGE=true already acknowledges."""
    doc = yaml.safe_load(_COMPOSE.read_text())
    env = doc["services"]["mcp"]["environment"]
    assert "FORWARDED_ALLOW_IPS" in env, (
        "the mcp service must set FORWARDED_ALLOW_IPS or uvicorn drops nginx's "
        "X-Forwarded-Proto and the app emits http:// absolute URLs"
    )
    # Exactly "*", not a pinned address. This service declares no `networks:` block, so
    # the bridge gateway address is implicit and can drift on a compose/docker change; a
    # stale pin would silently re-open the downgrade with no failing signal anywhere.
    assert str(env["FORWARDED_ALLOW_IPS"]) == "*", (
        "FORWARDED_ALLOW_IPS must be exactly '*' — a pinned bridge-gateway address can "
        f"drift and silently re-open the scheme downgrade; got {env['FORWARDED_ALLOW_IPS']!r}"
    )
