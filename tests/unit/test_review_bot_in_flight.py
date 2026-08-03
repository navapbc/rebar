"""``/health`` must report the in-flight review count (bug 34cd).

``infra/scripts/autodeploy.sh`` reads ``in_flight`` to decide whether recreating this container
would KILL a running review. That makes the field a load-bearing deploy contract, not a debugging
nicety: if it silently stopped counting a review path, the deploy loop would resume killing
reviews mid-flight — invisibly, because a killed review fails nothing and so emits no
``VOTER_ERROR`` and leaves ``restarts`` at 0.

The coverage that matters most is the RECONCILER path. uvicorn's shutdown drain waits on
``queue.join()``, which covers only webhook-queued events; the backfill reconciler awaits
``review_and_vote`` inline, so its review is cancelled outright on shutdown — and the reconciler
is the path that RETRIES a killed review. A busy signal blind to it would let the deploy loop
keep killing the very work meant to heal the gate.
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

    from rebar.review_bot.app import app

    monkeypatch.setattr(voter, "in_flight_reviews", lambda: 2)
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


def test_health_exposes_in_flight_without_needing_the_reviewbot_extra() -> None:
    """Same contract as the endpoint test above, asserted WITHOUT fastapi.

    The endpoint test is ``importorskip``-gated on the ``reviewbot`` extra, so in the default
    test tier it is skipped — and a skipped test would leave the deploy loop's parse target
    (``json.load(...)["in_flight"]``) unverified in exactly the tier that gates most changes.
    This reads the handler's source instead, so removing the field can never be a silent green.
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
    """The reconciler awaits ``review_and_vote`` INLINE, outside the webhook queue that
    uvicorn's shutdown drains — so it is the path a deploy silently kills, and the one the
    busy signal most needs to see.

    Asserted structurally (that reconcile calls the counted entry point) rather than by driving
    a whole backfill pass: what can regress is someone routing the reconciler at the uncounted
    ``_review_and_vote`` to skip the wrapper.
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
