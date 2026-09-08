"""Expose every running review through the ``/health`` in-flight count.

Autodeploy uses this field to avoid replacing a container during a review. The
count must include the reconciler, which awaits reviews outside the webhook
queue and therefore is not protected by the queue shutdown drain.
"""

from __future__ import annotations

import asyncio

import pytest

from rebar.review_bot import voter


def test_in_flight_is_zero_when_idle() -> None:
    assert voter.in_flight_reviews() == 0


def test_review_and_vote_is_counted_while_it_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The count must be held up for the DURATION of a review, not merely incremented."""
    observed: list[int] = []

    async def fake_review(event: dict, **kwargs: object) -> dict[str, str]:
        observed.append(voter.in_flight_reviews())
        return {"status": "skipped"}

    monkeypatch.setattr(voter, "_review_and_vote", fake_review)
    result = asyncio.run(voter.review_and_vote({}))

    assert observed == [1], (
        "a review must be counted in-flight while its body is executing — the deploy loop "
        f"samples this while the review runs, not before or after. observed={observed}"
    )
    assert result == {"status": "skipped"}, "the wrapper must pass the result through unchanged"
    assert voter.in_flight_reviews() == 0, "the count must return to 0 once the review completes"


def test_the_count_is_released_when_a_review_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A leaked count would read as "permanently busy" and, once the deferral bound expired,
    make every later deploy report an interrupted review that never existed."""

    async def boom(event: dict, **kwargs: object) -> dict[str, str]:
        raise RuntimeError("review blew up")

    monkeypatch.setattr(voter, "_review_and_vote", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(voter.review_and_vote({}))

    assert voter.in_flight_reviews() == 0, "an exception must not leak the in-flight count"


def test_the_count_is_released_when_a_review_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation is the NORMAL end for a review interrupted by a deploy or a timeout
    (``asyncio.wait_for`` cancels the inner coroutine), so it must not leak either."""

    async def hang(event: dict, **kwargs: object) -> dict[str, str]:
        await asyncio.sleep(60)
        return {"status": "voted"}

    monkeypatch.setattr(voter, "_review_and_vote", hang)

    async def scenario() -> None:
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await asyncio.wait_for(voter.review_and_vote({}), timeout=0.05)

    asyncio.run(scenario())
    assert voter.in_flight_reviews() == 0, "a cancelled review must not leak the in-flight count"


def test_concurrent_reviews_are_all_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The signal is a COUNT, not a boolean: the deferral must hold while any review runs."""
    peak = 0
    release = asyncio.Event()

    async def wait_for_release(event: dict, **kwargs: object) -> dict[str, str]:
        nonlocal peak
        peak = max(peak, voter.in_flight_reviews())
        await release.wait()
        return {"status": "skipped"}

    monkeypatch.setattr(voter, "_review_and_vote", wait_for_release)

    async def scenario() -> None:
        tasks = [asyncio.create_task(voter.review_and_vote({})) for _ in range(3)]
        await asyncio.sleep(0)  # let each task enter its body
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
    assert peak == 3, f"every concurrently-running review must be counted (peak={peak})"
    assert voter.in_flight_reviews() == 0


def test_health_endpoint_reports_the_in_flight_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deploy loop parses this payload with ``json.load(...)["in_flight"]``, so the field
    name and its integer type are the contract.

    Requires the ``reviewbot`` extra (fastapi); skipped without it.
    """
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rebar.review_bot import app as appmod
    from rebar.review_bot.app import app

    monkeypatch.setattr(voter, "in_flight_reviews", lambda: 2)
    monkeypatch.setattr(appmod, "_gerrit_auth_health", lambda _cfg: (True, "ok"), raising=False)
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["in_flight"] == 2, f"/health must expose the in-flight count\n{body}"
    assert isinstance(body["in_flight"], int) and not isinstance(body["in_flight"], bool), (
        f"in_flight must be an integer — autodeploy.sh compares it numerically\n{body}"
    )
    assert body["status"] == "ok", (
        f"the pre-existing liveness contract must be unchanged: the post-deploy readiness gate "
        f"and the host observability probe both still poll this route\n{body}"
    )


def test_health_endpoint_reports_the_queue_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Distinguish an absent event from one queued behind other reviews.

    ``in_flight`` counts running work while ``queue_depth`` exposes the backlog.
    Awaiting the handler directly verifies its returned response body without
    starting workers, the reconciler, the janitor, or the shutdown drain. The
    sibling test covers the HTTP surface. This test requires the ``reviewbot``
    extra.
    """
    pytest.importorskip("fastapi")
    from rebar.review_bot import app as appmod

    monkeypatch.setattr(voter, "in_flight_reviews", lambda: 0)
    monkeypatch.setattr(appmod, "_gerrit_auth_health", lambda _cfg: (True, "ok"), raising=False)

    # No lifespan has run in this test, so there may be no queue at all on app.state —
    # the field must still be present and 0, never absent.
    monkeypatch.delattr(appmod.app.state, "queue", raising=False)
    idle = asyncio.run(appmod.health())
    assert idle["queue_depth"] == 0, (
        f"with no queue yet, queue_depth must report 0, not be omitted\n{idle}"
    )

    backlog: asyncio.Queue = asyncio.Queue()
    for n in range(3):
        backlog.put_nowait({"type": "patchset-created", "_n": n})
    monkeypatch.setattr(appmod.app.state, "queue", backlog, raising=False)
    body = asyncio.run(appmod.health())

    assert body["queue_depth"] == 3, (
        f"/health must expose how many events are waiting to be reviewed\n{body}"
    )
    assert isinstance(body["queue_depth"], int) and not isinstance(body["queue_depth"], bool), (
        f"queue_depth must be an integer, like in_flight\n{body}"
    )
    # The pre-existing contract is additive-only: autodeploy.sh reads these two keys.
    assert body["status"] == "ok" and body["in_flight"] == 0


def test_health_endpoint_reports_degraded_when_gerrit_auth_is_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listening process is degraded if it cannot authenticate to cast votes."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rebar.review_bot import app as appmod

    monkeypatch.setattr(voter, "in_flight_reviews", lambda: 0)
    monkeypatch.setattr(
        appmod,
        "_gerrit_auth_health",
        lambda _cfg: (False, "gerrit_auth_failed:401"),
        raising=False,
    )
    monkeypatch.delattr(appmod.app.state, "queue", raising=False)

    with TestClient(appmod.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "in_flight": 0,
        "queue_depth": 0,
        "gerrit_auth": "failed",
        "reason": "gerrit_auth_failed:401",
    }


def test_gerrit_auth_health_checks_the_vote_casting_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    pytest.importorskip("fastapi")

    from rebar.review_bot import app as appmod
    from rebar.review_bot.config import ReceiverConfig
    from rebar.review_bot.gerrit_client import GerritError

    cfg = ReceiverConfig(dedup_db_path=str(tmp_path / "voted.db"), gerrit_bot_token="tok")
    calls: list[str] = []

    def ok(self) -> None:
        calls.append(self._cfg.gerrit_bot_token)

    monkeypatch.setattr(appmod.GerritClient, "check_auth", ok, raising=True)

    assert appmod._gerrit_auth_health(cfg) == (True, "ok")
    assert calls == ["tok"]
    assert appmod._gerrit_auth_health(ReceiverConfig(gerrit_bot_token="")) == (
        False,
        "gerrit_auth_missing_token",
    )

    def unauthorized(_self) -> None:
        raise GerritError("nope", status=401)

    monkeypatch.setattr(appmod.GerritClient, "check_auth", unauthorized, raising=True)
    assert appmod._gerrit_auth_health(cfg) == (False, "gerrit_auth_failed:401")


def test_gerrit_client_check_auth_uses_accounts_self_and_short_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from rebar.review_bot.config import ReceiverConfig
    from rebar.review_bot.gerrit_client import GerritClient

    client = GerritClient(ReceiverConfig(dedup_db_path=str(tmp_path / "voted.db")))
    calls: list[tuple[str, str, float]] = []

    def fake_request(method: str, path: str, *, timeout: float = 60, **_kwargs: object):
        calls.append((method, path, timeout))
        return 200, "{}"

    monkeypatch.setattr(client, "_request", fake_request)

    client.check_auth()

    assert calls == [("GET", "/a/accounts/self", 5)]


def test_health_exposes_in_flight_without_needing_the_reviewbot_extra() -> None:
    """Protect the ``in_flight`` response field without requiring FastAPI.

    The endpoint test may skip without the ``reviewbot`` extra. This structural
    check keeps autodeploy's parse target covered in the default test tier.
    """
    from pathlib import Path

    from rebar.review_bot import config as _config  # fastapi-free sibling module

    app_source = (Path(_config.__file__).parent / "app.py").read_text()
    assert '"in_flight": _voter.in_flight_reviews()' in app_source, (
        "the /health handler must report voter.in_flight_reviews() as `in_flight` — "
        "autodeploy.sh's drain gate parses that exact field, and treats its absence as an "
        "unreadable signal, which deploys blind."
    )
    assert '"status": "ok"' in app_source, "the liveness contract must remain unchanged"


def test_the_reconciler_path_is_counted_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Require the reconciler to call the counted review entry point.

    Reconciler work runs outside the drained webhook queue. This structural test
    rejects routing it directly to the uncounted ``_review_and_vote`` function.
    """
    from pathlib import Path

    from rebar.review_bot import reconcile

    source = Path(reconcile.__file__).read_text()
    assert "_review_and_vote" not in source, (
        "reconcile must not call the UNCOUNTED _review_and_vote — its reviews would then be "
        "invisible to the deploy loop's drain check, and the reconciler is exactly the path "
        "that retries a review a deploy already killed."
    )
    assert "review_and_vote" in source, "reconcile is expected to drive reviews"
