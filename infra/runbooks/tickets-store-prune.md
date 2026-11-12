# Runbook — reclaim unreachable objects in the production tickets store

**Ticket:** `successive-deceptive-xiphias` (`29b8-8d86-af5b-4608`). **Audience:** an operator with
SSM access to `i-00880b2c7f13527c5`. Commands run **on the instance** use its role and take no
`--profile`.

## Why this is manual

rebar's post-write maintenance step (`src/rebar/_store/event_commit_git.py`) packs objects and
expires reflogs under the store write lock, but never prunes unreachable objects. That needs a
reachability walk, and in this store one walk takes ~110 s: long enough to make every write
wait, and longer than writers wait for the lock (30 s x 2 attempts). The `loose-objects` task
also packs unreachable loose objects into `loose-*` packs, where `git prune` cannot reach them.

On 2026-11-12 the store held at most **157** unreachable objects of 1,071,347, accumulated since
the clone was made on 2026-08-26. The coordinator decided (on the ticket) to accept that growth
and reclaim it here only when it becomes material.

## 1. Measure (read-only, ~4 minutes)

Run on the host against the store's volume path:

```sh
cd /var/gerrit/site/mcp-tickets
git count-objects -v                                  # in-pack + count = objects stored
git rev-list --objects --all --reflog | wc -l         # objects still reachable (~110 s)
```

Unreachable objects are at most `(in-pack + count) - reachable`. It is an upper bound, because
an object can appear in more than one pack. Below a few thousand objects, stop here: there is
nothing worth a write pause.

## 2. Reclaim under the write lock (~10 minutes; writes fail meanwhile)

`git gc --cruft --prune=2.weeks.ago` drops unreachable objects older than 2 weeks from both
packs and loose storage. It rewrites every pack into one, moves young unreachable objects into
a cruft pack, and then prunes expired loose objects. A cruft `repack` alone is not enough:
it leaves expired loose objects on disk. Verified locally on git 2.56 (2026-11-12): a 30-day-old
unreachable loose object and a 30-day-old unreachable packed object were both removed, and a
fresh unreachable object was kept. The container's git (2.47) supports `--cruft`.

It is a full repack, measured at 605.6 s on this store (bug `nickelic-lazuline-gonolek`).
While it runs it holds rebar's write lock, so any ticket write in that window fails with a lock
timeout and must be retried. Pick a quiet window and tell anyone who might be writing.

Run it **inside the live MCP container**, through rebar's own lock, so it never races a writer:

```sh
mcp=$(docker ps --format '{{.Names}}' | grep '^rebar-mcp-' | head -1)
docker exec "$mcp" python -c '
import subprocess, time
from rebar._store.lock import write_lock
store = "/var/gerrit/site/mcp-tickets"
with write_lock(store, timeout=120):
    t0 = time.time()
    subprocess.run(
        ["git", "-C", store, "-c", "pack.threads=1", "gc", "--cruft", "--prune=2.weeks.ago"],
        check=True,
    )
    print(f"gc done in {time.time() - t0:.0f}s")
'
```

`pack.threads=1` keeps the repack's memory down: the container's memory limit is shared with
the server itself. `gc` also expires reflogs under the same `gc.reflogExpire*` policy the
post-write step already applies, so that adds nothing new. If `write_lock` raises
`LockTimeout`, a writer was mid-operation: rerun the command.

## 3. Verify

Repeat §1. The upper bound should fall to roughly the number of unreachable objects younger
than 2 weeks. Then make one ordinary ticket write (a comment through the MCP server) and
confirm it succeeds.

## If writes fail on a stranded ref lock

The post-write step's daily `git reflog expire` runs with a 30 s bound. If it is ever killed, git can
leave a ref lock behind, and rebar logs an ERROR (`git reflog expire timed out ... stranded
ref lock`) naming the store's git dir. Every later write then fails on that lock. To recover:

1. Confirm no git process is running in the store: `docker exec "$mcp" pgrep -a git`
   (expect nothing, or only processes you can identify as finishing normally).
2. Find the ref locks under the git dir from the ERROR line:
   `docker exec "$mcp" find <git dir>/HEAD.lock <git dir>/packed-refs.lock <git dir>/refs <git dir>/logs -name '*.lock' -ls 2>/dev/null`.
   Check each one's age: a lock seconds old may belong to a live operation; wait and recheck.
3. Remove only those old lock files: `docker exec "$mcp" rm <lock path>`.
4. Make one ticket write and confirm it succeeds.

## Rollback

None is needed: `gc` removes only objects that no ref or reflog reaches and that are older
than 2 weeks. If it fails part-way, git leaves the existing packs in place, so the store is
still complete; rerun §2.
