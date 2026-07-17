"""Standalone worker for the enrich-queue prune concurrency regression test.

Run as a SEPARATE process (faithful to concurrent ``rebar review-plan`` invocations):
  writer <store> <ticket> <bursts>        — locked append_event bursts (SIGNATURE+REVIEW_RESULT)
  pruner <store> <rounds> <ticket>...     — the enrich-queue prune committer, sustained per round

A writer prints its swallowed-raise count to stdout. Push is forced off (single-store race).
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    role, store = sys.argv[1], sys.argv[2]
    os.environ["REBAR_SYNC_PUSH"] = "off"
    # This test exercises the prune-vs-locked-write concurrency invariant (I5), which is
    # orthogonal to authorship enforcement. Keep the write-gate OFF so the fresh (unsigned)
    # store's writes are never REFUSED under a repo-level identity.require_authenticated=true —
    # otherwise a correctly-refused unsignable write would masquerade as a prune-dropped write.
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
                    # Diagnostic (bug ac26 residual): surface WHY the write was lost — the
                    # writer's underlying git stderr is wrapped into the raised StoreError.
                    # stdout carries ONLY the count the test parses, so emit the full detail
                    # to stderr with a greppable marker for the failing-CI capture.
                    print(
                        f"SWALLOWED_WRITE_RAISE {et} j={j}: {exc!r}\n{traceback.format_exc()}",
                        file=sys.stderr,
                    )
        print(raised)
    else:  # pruner
        rounds, tids = int(sys.argv[3]), sys.argv[4:]
        from rebar.llm.enrich_drain import _prune_queue_events
        from rebar.llm.overlap import queue as _queue

        t = str(tracker)
        for _ in range(rounds):
            for tid in tids:
                try:
                    _queue.enqueue(tid, soak_min=0, repo_root=store)
                    _prune_queue_events(tid, t)
                except BaseException:  # noqa: BLE001 — the drain swallows prune failures
                    pass


if __name__ == "__main__":
    main()
