"""Standalone worker for the purge-bridge deletion-commit concurrency regression test.

Run as a SEPARATE process (faithful to a real ``rebar purge-bridge`` invocation racing
concurrent locked ``rebar`` writes on the same store):

  writer <store> <ticket> <bursts>            — locked append_event bursts (SIGNATURE+REVIEW_RESULT)
  purger <store> <keep> <rounds> <name>...     — sustained purge-bridge deletion committer

A writer prints its swallowed-raise count to stdout and the swallowed exceptions to stderr
(mirrors the enrich sibling). Push is forced off (single-store race).

Each purger round re-creates its assigned jira-* ticket dirs (each with a non-``keep`` Jira
project key so purge deletes them) using a *pathspec-scoped* commit — never a whole-index
``add -A`` — so ONLY ``purge_bridge._commit_deletion`` runs the whole-index commit whose
locking is under test. Then it invokes the real ``purge_bridge_cli`` which deletes those dirs
and commits the removal (the seam the merged I5 fix locked + pathspec-scoped).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def _git(tracker: str, *args: str) -> None:
    subprocess.run(["git", "-C", tracker, *args], capture_output=True, text=True)


def main() -> None:
    role, store = sys.argv[1], sys.argv[2]
    os.environ["REBAR_SYNC_PUSH"] = "off"
    # This test exercises the purge-vs-locked-write concurrency invariant (I5), orthogonal to
    # authorship enforcement. Keep the write-gate OFF so the fresh (unsigned) store's writes are
    # never REFUSED under identity.require_authenticated=true — otherwise a correctly-refused
    # unsignable write would masquerade as a purge-dropped write.
    os.environ["REBAR_IDENTITY_REQUIRE_AUTHENTICATED"] = "0"
    from rebar import config

    tracker = config.tracker_dir(store)
    if role == "writer":
        import traceback

        tid, bursts = sys.argv[3], int(sys.argv[4])
        from rebar._commands._seam import append_event

        raised = 0
        for j in range(bursts):
            for et in ("SIGNATURE", "REVIEW_RESULT"):
                try:
                    append_event(
                        tid,
                        et,
                        {"schema": "plan_review_result_v1", "j": j, "et": et},
                        tracker,
                        repo_root=store,
                    )
                except BaseException as exc:  # noqa: BLE001 — mirror the swallow at the review call sites
                    raised += 1
                    # Surface WHY the write was lost — the writer's underlying git stderr is
                    # wrapped into the raised StoreError. stdout carries ONLY the count the test
                    # parses, so emit the full detail to stderr with a greppable marker.
                    print(
                        f"SWALLOWED_WRITE_RAISE {et} j={j}: {exc!r}\n{traceback.format_exc()}",
                        file=sys.stderr,
                    )
        print(raised)
    else:  # purger
        keep, rounds, names = sys.argv[3], int(sys.argv[4]), sys.argv[5:]
        tracker = str(tracker)
        from rebar._commands.purge_bridge import purge_bridge_cli
        from rebar._store import lock as _lock

        for r in range(rounds):
            for name in names:
                tdir = os.path.join(tracker, f"{name}-{r}")
                os.makedirs(tdir, exist_ok=True)
                # Non-``keep`` Jira project key => purge_bridge will delete this dir.
                with open(os.path.join(tdir, "0001-CREATE.json"), "w", encoding="utf-8") as fh:
                    json.dump({"data": {"jira_key": "DEL-1"}}, fh)
                rel = os.path.relpath(tdir, tracker)
                # Seed the deletable jira dir the same lock-safe, pathspec-scoped way every
                # canonical write commits: HOLD the write lock (so this harness step never
                # itself races a writer) and stage/commit ONLY this dir (never ``add -A``). That
                # isolates purge_bridge._commit_deletion as the sole seam whose locking is tested.
                with _lock.write_lock(tracker, dual_window=True):
                    _git(tracker, "add", "--", rel)
                    _git(tracker, "commit", "-q", "--no-verify", "-m", f"seed {rel}", "--", rel)
            try:
                purge_bridge_cli([f"--keep={keep}"], repo_root=store)
            except BaseException:  # noqa: BLE001 — mirror the drain: purge failures are swallowed
                pass


if __name__ == "__main__":
    main()
