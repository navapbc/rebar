"""A `/health` 200 must mean this container has already SERVED an MCP request.

WHY THIS TEST EXISTS. `/health` is a FastMCP `custom_route` registered deliberately OUTSIDE the
auth middleware and the DNS-rebinding transport-security guard, and `infra/scripts/autodeploy.sh`
flips the nginx `/mcp` upstream the moment it returns 200 — so the deploy path promoted
containers that had never served one `/mcp` request (measured on the box: promoted 54 s before
its first). A container whose `REBAR_MCP_HTTP_ALLOWED_HOSTS` does not admit the hostname real
traffic carries answers `/health` 200 while 421-ing every real request, and the cutover promotes
it (bug vaccinated-flavorous-solenodon).

THE DESIGN THIS PINS. The server drives ONE real `initialize` through its own
`StreamableHTTPSessionManager` inside the ASGI lifespan — after the session manager is running,
before Starlette answers `lifespan.startup.complete`, which uvicorn waits for before accepting a
connection — and reports the outcome as `/health`'s `handshake` field. Two properties are in
tension and both are required: CONCLUSIVE (a pass could not have been produced without the real
request path working) and BOUNDED (a failure or a hang must not wedge startup).

THE ANCHORING RULE these tests obey. This bug IS a vacuous health check, so an assertion that
would pass when the handshake never ran is the defect restated. Every test below first proves the
drive actually executed — a counter, or the record's presence — and only then asserts its
outcome; and both directions are covered, because a one-sided test cannot tell "the check works"
from "the check always passes".
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import pytest

from rebar._mcp_health import (
    _HANDSHAKE_ATTR,
    DEFAULT_HANDSHAKE_BUDGET_SECONDS,
    drive_initialize,
    handshake_status,
    install_startup_handshake,
    run_startup_handshake,
    select_probe_host,
    wire_health,
)

pytestmark = pytest.mark.unit

PUBLIC_HOST = "rebar.solutions.navateam.com"


def _server(allowed_hosts: list[str], resource_url: str | None = None) -> Any:
    """A FastMCP built the way `build_server` builds the HTTP transport: a non-loopback bind,
    stateless HTTP, and DNS-rebinding protection on with an explicit allowlist."""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    auth = None
    verifier = None
    if resource_url is not None:
        # AuthSettings is the SDK's home for the declared public resource URL, and the SDK
        # refuses it without a verifier — so build the same pair `build_server` builds. The
        # verifier is never reached: the probe drives the session manager, not the route.
        from mcp.server.auth.provider import TokenVerifier
        from mcp.server.auth.settings import AuthSettings

        class _NoTokens(TokenVerifier):  # pragma: no cover - never invoked by the probe
            async def verify_token(self, token: str) -> None:
                return None

        auth = AuthSettings(issuer_url=resource_url, resource_server_url=resource_url)
        verifier = _NoTokens()
    mcp = FastMCP(
        "rebar-handshake-test",
        auth=auth,
        token_verifier=verifier,
        host="0.0.0.0",  # the box binds the wildcard; that is the case under test
        port=8091,
        streamable_http_path="/mcp",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(allowed_hosts),
            allowed_origins=[f"https://{PUBLIC_HOST}"],
        ),
    )
    wire_health(mcp)
    return mcp


def _counted_real_drive(mcp: Any, calls: list[str]) -> Any:
    """The REAL drive, wrapped in a counter. The counter is the anchor (it proves the drive ran);
    the status still comes from the server's own session manager, so the assertion is not
    weakened by the injection."""

    async def _drive() -> int | None:
        probe_host = select_probe_host(
            mcp.settings.transport_security,
            resource_server_url=getattr(mcp.settings.auth, "resource_server_url", None),
            bind_host=mcp.settings.host,
            bind_port=mcp.settings.port,
        )
        calls.append(probe_host)
        return await drive_initialize(
            mcp.session_manager, probe_host=probe_host, path="/mcp", bind_port=8091
        )

    return _drive


def test_correctly_configured_server_reports_a_successful_handshake() -> None:
    """A server whose allowlist admits the host real traffic carries serves its own probe."""
    from starlette.testclient import TestClient

    mcp = _server([PUBLIC_HOST])
    app = mcp.streamable_http_app()
    calls: list[str] = []
    install_startup_handshake(mcp, app, drive=_counted_real_drive(mcp, calls))

    with TestClient(app) as client:
        health = client.get("/health").json()

    assert calls == [PUBLIC_HOST], f"the drive must actually run, once: {calls}"
    record = health["handshake"]
    assert record["ok"] is True, record
    assert record["status"] == 200, record
    assert record["host"] == PUBLIC_HOST, record
    assert isinstance(record["elapsed_ms"], float), record


def test_the_uninjected_production_drive_reaches_the_real_request_path() -> None:
    """The SHIPPED drive — no test double anywhere — gets a 200 out of the session manager."""
    from starlette.testclient import TestClient

    mcp = _server([PUBLIC_HOST])
    app = mcp.streamable_http_app()
    install_startup_handshake(mcp, app)

    with TestClient(app) as client:
        record = client.get("/health").json()["handshake"]
        # Anchor the OTHER direction too: the same request, driven from outside, agrees.
        live = client.post(
            "/mcp",
            headers={
                "host": PUBLIC_HOST,
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "probe", "version": "1"},
                    },
                }
            ),
        )

    assert record["ok"] is True and record["status"] == 200, record
    assert live.status_code == record["status"], (
        f"the handshake must report what a real request gets: {record} vs {live.status_code}"
    )


def test_unadmitted_probe_host_is_reported_as_failed_with_421() -> None:
    """THE NEGATIVE DIRECTION. `*.example.com` is a form `_validate_host` can never match, so this
    server admits NOTHING usable: the probe falls back to its bind address and its own guard
    refuses it. `/health` must report that refusal rather than report ready."""
    from starlette.testclient import TestClient

    mcp = _server(["*.example.com"])
    app = mcp.streamable_http_app()
    calls: list[str] = []
    install_startup_handshake(mcp, app, drive=_counted_real_drive(mcp, calls))

    with TestClient(app) as client:
        health = client.get("/health").json()

    assert calls == ["127.0.0.1:8091"], f"the drive must actually run, once: {calls}"
    record = health["handshake"]
    assert record["ok"] is False, record
    assert record["status"] == 421, f"the REFUSING status must be reported verbatim: {record}"
    assert "421" in str(record["error"]), record


def test_a_hanging_drive_is_bounded_by_the_budget() -> None:
    """AC 3, the hang half. A drive that never returns terminates on the budget — proved with an
    injected clock and a zero budget, so the test does no real sleeping and cannot flake."""
    import anyio

    entered: list[str] = []
    ticks = iter([100.0, 100.25])

    async def _never_returns() -> int:
        entered.append("started")
        await anyio.sleep_forever()
        raise AssertionError("unreachable")  # pragma: no cover

    async def _run() -> dict[str, Any]:
        return await run_startup_handshake(
            _never_returns,
            probe_host=PUBLIC_HOST,
            budget_seconds=0,
            monotonic=lambda: next(ticks),
        )

    record = anyio.run(_run)

    assert entered == ["started"], "the hang case must actually enter the drive"
    assert record["ok"] is False and record["status"] is None, record
    assert "budget" in str(record["error"]), record
    assert record["elapsed_ms"] == 250.0, (
        f"elapsed must come from the injected clock, not wall time: {record}"
    )


def test_the_budget_is_handed_to_the_timeout_scope() -> None:
    """The bound is the module constant, and it reaches the timeout scope. Injecting the scope
    proves the wiring without waiting for any clock at all."""
    import anyio

    handed: list[float] = []

    @contextlib.contextmanager
    def _fake_fail_after(seconds: float) -> Any:
        handed.append(seconds)
        raise TimeoutError

    async def _unreached() -> int:  # pragma: no cover - the scope raises before the body
        raise AssertionError("the drive must not run when the scope has already expired")

    record = anyio.run(
        lambda: run_startup_handshake(
            _unreached, probe_host=PUBLIC_HOST, fail_after=_fake_fail_after
        )
    )

    assert handed == [DEFAULT_HANDSHAKE_BUDGET_SECONDS], handed
    assert record["ok"] is False and "budget" in str(record["error"]), record


def test_a_raising_drive_is_reported_not_propagated() -> None:
    """AC 3, the raise half: the failure is reported and boot continues."""
    import anyio

    async def _boom() -> int:
        raise RuntimeError("boom")

    record = anyio.run(lambda: run_startup_handshake(_boom, probe_host=PUBLIC_HOST))

    assert record["ok"] is False, record
    assert "RuntimeError: boom" in str(record["error"]), record


def test_a_failed_handshake_still_lets_the_lifespan_start() -> None:
    """Boot continues: a raising drive does not abort startup, and /health still answers."""
    from starlette.testclient import TestClient

    mcp = _server([PUBLIC_HOST])
    app = mcp.streamable_http_app()

    async def _boom() -> int:
        raise RuntimeError("boom")

    install_startup_handshake(mcp, app, drive=_boom)

    with TestClient(app) as client:
        health = client.get("/health")

    assert health.status_code == 200
    assert health.json()["handshake"]["ok"] is False, health.json()


def test_handshake_is_recorded_before_lifespan_startup_complete() -> None:
    """AC 4. Driven through the RAW ASGI lifespan protocol: the record must already exist when
    `lifespan.startup.complete` is emitted, because uvicorn accepts its first connection only
    after receiving that message."""
    import anyio

    mcp = _server([PUBLIC_HOST])
    app = mcp.streamable_http_app()
    calls: list[str] = []
    install_startup_handshake(mcp, app, drive=_counted_real_drive(mcp, calls))

    observed: list[tuple[str, bool]] = []

    async def _drive_lifespan() -> None:
        messages = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
        index = 0

        async def receive() -> dict[str, Any]:
            nonlocal index
            message = messages[index]
            index += 1
            return message

        async def send(message: dict[str, Any]) -> None:
            observed.append(
                (message["type"], isinstance(getattr(mcp, _HANDSHAKE_ATTR, None), dict))
            )

        await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)

    anyio.run(_drive_lifespan)

    assert calls == [PUBLIC_HOST], f"the drive must actually run: {calls}"
    assert observed, "the lifespan must have emitted at least one message"
    assert observed[0] == ("lifespan.startup.complete", True), (
        f"the handshake must be recorded BEFORE startup completes: {observed}"
    )


def test_install_is_a_no_op_without_a_starlette_app() -> None:
    """Installation never breaks a boot. `run_http_with_grace` has another direct caller whose
    fake server returns a bare `object()` from `streamable_http_app()` and carries no
    `settings.auth` (tests/unit/test_mcp_opcert_signing_goose.py) — installing against that must
    return the app untouched rather than raise."""
    from types import SimpleNamespace

    app = object()
    fake = SimpleNamespace(settings=SimpleNamespace(host="127.0.0.1", port=0, log_level="INFO"))

    assert install_startup_handshake(fake, app) is app
    assert handshake_status(fake)["ok"] is False


def test_handshake_status_is_fail_closed_when_the_handshake_never_ran() -> None:
    """A server that never ran the handshake reports ok=False, not a missing field: the deploy
    gate must not read silence as success."""

    class _Bare:
        pass

    record = handshake_status(_Bare())

    assert record["ok"] is False and record["status"] is None, record


@pytest.mark.parametrize(
    ("allowed_hosts", "resource_url", "expected"),
    [
        # (1) the DECLARED public host wins over anything in the allowlist — that independence
        # is what lets the probe catch an allowlist pointing at the wrong hostname.
        (["other.example.com"], f"https://{PUBLIC_HOST}/mcp", PUBLIC_HOST),
        # a non-default port is part of the Host a proxy sends; a default one is not.
        ([], "http://box.internal:8443/mcp", "box.internal:8443"),
        ([], f"https://{PUBLIC_HOST}/mcp", PUBLIC_HOST),
        # (2)-(4): no declared URL -> literal entry, then a materialized `host:*`, then the bind.
        ([PUBLIC_HOST, "127.0.0.1:8091"], None, PUBLIC_HOST),
        (["example.com:*"], None, "example.com:8091"),
        (["*.example.com"], None, "127.0.0.1:8091"),
        ([], None, "127.0.0.1:8091"),
    ],
)
def test_select_probe_host_precedence(
    allowed_hosts: list[str], resource_url: str | None, expected: str
) -> None:
    """Declared public host first, then a literal allowlist entry, a materialized `host:*`, and
    finally the bind with the wildcard normalized — the fallback that makes an unusable allowlist
    detectable."""
    from mcp.server.transport_security import TransportSecuritySettings

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts
    )

    assert (
        select_probe_host(
            security,
            resource_server_url=resource_url,
            bind_host="0.0.0.0",  # the box binds the wildcard
            bind_port=8091,
        )
        == expected
    )


def test_allowlist_that_admits_the_wrong_hostname_fails() -> None:
    """THE FIELD CASE. The allowlist is non-empty and perfectly valid — it simply admits a
    DIFFERENT hostname than the one this deployment declares as its public resource URL. `/health`
    still returns 200; the handshake must report the 421 that every real request would get."""
    from starlette.testclient import TestClient

    mcp = _server(["stale.example.com"], resource_url=f"https://{PUBLIC_HOST}/mcp")
    app = mcp.streamable_http_app()
    calls: list[str] = []
    install_startup_handshake(mcp, app, drive=_counted_real_drive(mcp, calls))

    with TestClient(app) as client:
        health = client.get("/health").json()

    assert calls == [PUBLIC_HOST], f"the probe must present the DECLARED public host: {calls}"
    assert health["handshake"]["ok"] is False, health["handshake"]
    assert health["handshake"]["status"] == 421, health["handshake"]


def test_run_http_with_grace_hands_uvicorn_an_app_that_runs_the_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PRODUCTION WIRING, not just the helper: the app `run_http_with_grace` hands to uvicorn
    must be the one whose lifespan runs the handshake. Without this, deleting the install call
    would leave every unit test green while shipping the original bug.

    A fake uvicorn Config/Server keeps the test port-free (the pattern
    test_run_http_with_grace_installs_sigterm_and_bounds_uvicorn uses); the captured app is then
    driven through the raw ASGI lifespan protocol."""
    import signal

    import anyio

    import rebar._mcp_health as health

    captured: dict[str, Any] = {}

    class _FakeConfig:
        def __init__(self, app: Any, **kwargs: Any) -> None:
            captured["app"] = app

    class _FakeServer:
        def __init__(self, config: Any) -> None:
            self.should_exit = False

        def run(self) -> None:  # returns at once → the serving thread ends immediately
            captured["ran"] = True

    monkeypatch.setattr("uvicorn.Config", _FakeConfig)
    monkeypatch.setattr("uvicorn.Server", _FakeServer)

    mcp = _server([PUBLIC_HOST])
    previous = signal.getsignal(signal.SIGTERM)
    try:
        health.run_http_with_grace(mcp, getattr(mcp, health._GAUGE_ATTR), grace_seconds=1)
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert captured.get("ran") is True, "the fake uvicorn server must have been run"

    async def _drive_lifespan() -> None:
        messages = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
        index = 0

        async def receive() -> dict[str, Any]:
            nonlocal index
            message = messages[index]
            index += 1
            return message

        async def send(_message: dict[str, Any]) -> None:
            return None

        await captured["app"]({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)

    anyio.run(_drive_lifespan)

    record = handshake_status(mcp)
    assert record["ok"] is True and record["status"] == 200, record
