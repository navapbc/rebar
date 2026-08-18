"""Concurrency oracle for RP-04 S3 child-env projection (ticket 6e3b, AC4).

AC4: *Concurrent-operation tests prove no global environment mutation and no
cross-operation credential/config observation.* The ticket's Testing section requires
"Concurrency tests run distinct operations with different overlays and timeouts,
asserting ``os.environ`` is byte-equal before/during/after and no cross-operation value
is observed."

``_child_env.project_child_env`` is a pure function that returns a fresh mapping and never
touches the global environment, so distinct operations may project concurrently with
per-operation overlays without any interference. These tests exercise that invariant under
real thread contention: each worker owns a distinct secret value, and the tests assert (a)
each worker observes ONLY its own overlaid credential, never a sibling's, and (b) the
global ``os.environ`` is byte-for-byte identical before, during, and after the storm.

Observable behavior only — returned mappings and the (non-)mutation of ``os.environ``.
"""

from __future__ import annotations

import threading

from _subprocess_env import subprocess_env

from rebar import _child_env


def _snapshot_environ() -> dict[str, str]:
    return subprocess_env()


def test_concurrent_owning_projections_isolate_credentials_without_touching_os_environ(
    monkeypatch,
) -> None:
    """Distinct owning operations run concurrently; each sees only its own overlaid
    secret, an ambient sentinel never leaks in, and ``os.environ`` is byte-equal
    before/during/after."""
    # An ambient credential that owning projections must NOT inherit (it comes only from
    # each operation's overlay). Present in os.environ so we also prove global
    # non-mutation against a realistic, secret-bearing environment.
    monkeypatch.setenv("JIRA_API_TOKEN", "AMBIENT-SENTINEL-MUST-NOT-LEAK")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o Foo=bar")  # native var must survive

    before = _snapshot_environ()
    base = subprocess_env()

    n_workers = 12
    iterations = 200
    barrier = threading.Barrier(n_workers + 1)
    errors: list[str] = []
    errors_lock = threading.Lock()
    stop = threading.Event()

    def record(msg: str) -> None:
        with errors_lock:
            errors.append(msg)

    def worker(idx: int) -> None:
        my_token = f"op-{idx}-secret"
        other_tokens = {f"op-{j}-secret" for j in range(n_workers) if j != idx}
        barrier.wait()
        for _ in range(iterations):
            if stop.is_set():
                return
            env = _child_env.project_child_env(
                base,
                relationship="owning",
                owner="jira",
                overlay={"JIRA_API_TOKEN": my_token},
            )
            # Own credential comes ONLY from the overlay.
            if env.get("JIRA_API_TOKEN") != my_token:
                record(f"worker {idx}: expected own token, got {env.get('JIRA_API_TOKEN')!r}")
                return
            # The ambient sentinel must never be inherited by an owning child.
            if env.get("JIRA_API_TOKEN") == "AMBIENT-SENTINEL-MUST-NOT-LEAK":
                record(f"worker {idx}: ambient sentinel leaked into owning child")
                return
            # No sibling operation's credential is ever observed.
            if env["JIRA_API_TOKEN"] in other_tokens:
                record(f"worker {idx}: observed a cross-operation credential")
                return
            # Native, non-adapter variables survive projection.
            if env.get("GIT_SSH_COMMAND") != "ssh -o Foo=bar":
                record(f"worker {idx}: native GIT_SSH_COMMAND did not survive")
                return

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    barrier.wait()  # release all workers at once for maximum contention

    # Assert the global environment is unmutated WHILE the operations run.
    for _ in range(50):
        assert _snapshot_environ() == before, "os.environ mutated during concurrent projection"

    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "a projection worker did not finish within its timeout"

    stop.set()
    assert errors == [], f"cross-operation / leak failures: {errors}"
    assert _snapshot_environ() == before, "os.environ changed after concurrent projection"


def test_concurrent_unrelated_siblings_strip_secrets_under_contention(monkeypatch) -> None:
    """Unrelated-sibling projections concurrently strip every adapter secret NAME while
    the global environment stays byte-equal throughout."""
    monkeypatch.setenv("JIRA_API_TOKEN", "cloud-secret")
    monkeypatch.setenv("JIRA_PAT", "dc-secret")
    monkeypatch.setenv("AWS_PROFILE", "native-should-survive")

    before = _snapshot_environ()
    base = subprocess_env()
    owned = _child_env.owned_secret_names()

    n_workers = 10
    iterations = 200
    barrier = threading.Barrier(n_workers)
    errors: list[str] = []
    errors_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        for _ in range(iterations):
            env = _child_env.project_child_env(base, relationship="unrelated")
            leaked = owned & env.keys()
            if leaked:
                with errors_lock:
                    errors.append(f"unrelated sibling retained adapter secrets: {sorted(leaked)}")
                return
            if env.get("AWS_PROFILE") != "native-should-survive":
                with errors_lock:
                    errors.append("native AWS_PROFILE did not survive unrelated projection")
                return

    threads = [threading.Thread(target=worker) for _ in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "an unrelated-sibling worker did not finish within its timeout"

    assert errors == [], f"secret-strip failures under contention: {errors}"
    assert _snapshot_environ() == before, "os.environ changed under concurrent unrelated projection"
