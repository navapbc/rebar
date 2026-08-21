"""Happy-path contract for opt-in strict tickets-branch delivery."""

from __future__ import annotations

import subprocess

import pytest

from rebar import config
from rebar._store import push

pytestmark = pytest.mark.unit


def test_strict_policy_decline_raises_catchable_delivery_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strict callers get a typed failure while the legacy default still returns."""
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    monkeypatch.setattr(push, "_push_mode", lambda _root=None: "always")
    monkeypatch.setattr(config, "tickets_branch", lambda _root=None: "tickets")
    monkeypatch.setattr(config, "tickets_remote", lambda _root=None: "origin")

    def fake_git(_base: str, *args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("remote", "get-url"):
            return subprocess.CompletedProcess(args, 0, "local-origin\n", "")
        if args and args[0] == "push":
            return subprocess.CompletedProcess(
                args,
                1,
                "",
                "remote: error: GH013 rule violation\n"
                "! [remote rejected] HEAD -> tickets (pre-receive hook declined)",
            )
        if args[:2] == ("rev-list", "--count"):
            return subprocess.CompletedProcess(args, 0, "3\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(push, "_git", fake_git)

    with pytest.raises(Exception) as caught:
        push.push_tickets_branch(str(tracker), strict=True)

    error = caught.value
    assert type(error).__name__ == "PushDeliveryError"
    assert getattr(error, "reason", None) == "push-policy-declined"
    assert "GH013" in str(error)
    assert "3 unpushed commits on the local tickets branch ahead of origin/tickets" in str(error)

    assert push.push_tickets_branch(str(tracker)) is None
