#!/bin/sh
# Converge a single-branch `tickets` clone into a writable rebar store on each
# container start. A fresh clone needs both Git identity and rebar's ignored registry.
#
# Contract (target dir = $1, else $REVIEWBOT_TICKETS_DIR):
#   * no-op when the clone is deferred or absent;
#   * set a repo-local git identity (user.email / user.name), overridable via
#     REVIEWBOT_GIT_USER_EMAIL / REVIEWBOT_GIT_USER_NAME;
#   * reconcile by merge-as-union, with guarded pre-epoch adoption only;
#   * run the idempotent registry ensures. REVIEWBOT_PYTHON selects the interpreter.
set -eu

DIR="${1:-${REVIEWBOT_TICKETS_DIR:-}}"
# Attribute writes to the configurable Rebar Bot identity.
EMAIL="${REVIEWBOT_GIT_USER_EMAIL:-joeoakhart+bot@navapbc.com}"
NAME="${REVIEWBOT_GIT_USER_NAME:-Rebar Bot}"
PY="${REVIEWBOT_PYTHON:-python3}"

if [ -z "$DIR" ]; then
	echo "reviewbot-ensure-tickets: REVIEWBOT_TICKETS_DIR unset; nothing to do" >&2
	exit 0
fi

# A deferred clone is not yet an error to converge.
if [ ! -d "$DIR/.git" ]; then
	echo "reviewbot-ensure-tickets: $DIR is not a git clone yet (clone deferred); skipping" >&2
	exit 0
fi

# Keep identity on the target clone as well as the one-clone container below.
git -C "$DIR" config user.email "$EMAIL"
git -C "$DIR" config user.name "$NAME"

# rebar resolves attribution from its source root, so the one-clone container also
# needs the same global identity. Do not reuse this pattern in a multi-clone container.
git config --global user.email "$EMAIL"
git config --global user.name "$NAME"

# Reconcile by merge-as-union. A clean pre-epoch clone may adopt only the immutable
# remote commit fetched here, after proving it had no local-only history.
LOCAL_HEAD="$(git -C "$DIR" rev-parse --verify "HEAD^{commit}" 2>/dev/null || true)"
PRIOR_REMOTE="$(git -C "$DIR" rev-parse --verify "refs/remotes/origin/tickets^{commit}" 2>/dev/null || true)"
if ! git -C "$DIR" fetch --quiet origin "+refs/heads/tickets:refs/remotes/origin/tickets"; then
	echo "reviewbot-ensure-tickets: tickets fetch failed; remote unavailable and convergence deferred; preserving local HEAD ${LOCAL_HEAD:-unreadable}" >&2
else
	PINNED_REMOTE="$(git -C "$DIR" rev-parse --verify "refs/remotes/origin/tickets^{commit}" 2>/dev/null || true)"
	if [ -z "$PINNED_REMOTE" ]; then
		echo "reviewbot-ensure-tickets: tickets remote ref is unreadable after fetch; manual intervention required; preserving local HEAD ${LOCAL_HEAD:-unreadable}" >&2
	else
		if ! "$PY" - "$DIR" "$LOCAL_HEAD" "$PRIOR_REMOTE" "$PINNED_REMOTE" <<'PY'
import subprocess
import sys

from rebar._store import compat, lock, sync

tracker, local_before, prior_remote, pinned_remote = sys.argv[1:]


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", tracker, *args], check=False, capture_output=True, text=True
    )


def warn(detail: str) -> None:
    print(
        "reviewbot-ensure-tickets: manual intervention required; "
        f"preserving local HEAD {local_before or 'unreadable'}: {detail}",
        file=sys.stderr,
    )


local_epoch, local_problem = compat._local_store_epoch(tracker)
remote_epoch, remote_problem = compat._remote_store_epoch(tracker, pinned_remote)
if local_problem or remote_problem:
    warn(local_problem or remote_problem or "unreadable store epoch")
elif local_epoch == remote_epoch:
    # Compatible histories take the union path.
    sync.reconverge(tracker)
elif local_epoch is not None or remote_epoch is None:
    warn(
        "store epoch mismatch is not a pre-epoch-to-epoch adoption "
        f"(local={local_epoch!r}, remote={remote_epoch!r})"
    )
elif not prior_remote:
    warn("prior origin/tickets ref was unavailable before fetch")
elif not local_before:
    warn("local HEAD was unavailable before fetch")
elif git("merge-base", "--is-ancestor", local_before, prior_remote).returncode != 0:
    warn("local history was ahead of or diverged from the prior origin/tickets ref")
else:
    # Re-check adoption preconditions under rebar's unified write lock.
    try:
        with lock.write_lock(tracker, attempts=1, dual_window=True):
            lock.check_no_rebase_in_progress(tracker)
            current = git("rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
            if current != local_before:
                warn("local HEAD changed while waiting for the store write lock")
            elif git("diff", "--quiet").returncode or git("diff", "--cached", "--quiet").returncode:
                warn("working tree has uncommitted changes")
            else:
                current_epoch, current_problem = compat._local_store_epoch(tracker)
                pinned_epoch, pinned_problem = compat._remote_store_epoch(tracker, pinned_remote)
                if current_problem or pinned_problem:
                    warn(current_problem or pinned_problem or "unreadable store epoch")
                elif current_epoch is not None or not isinstance(pinned_epoch, str):
                    warn("epoch adoption preconditions changed while waiting for the write lock")
                else:
                    # Adopt the immutable fetched commit, never a moving tracking ref.
                    adoption_target = pinned_remote
                    if git("reset", "--hard", "--quiet", adoption_target).returncode != 0:
                        warn(f"could not adopt pinned remote commit {pinned_remote}")
                    else:
                        print(
                            "reviewbot-ensure-tickets: adopted pre-epoch local HEAD "
                            f"{local_before} to reclaimed epoch tip {pinned_remote}",
                            file=sys.stderr,
                        )
    except (compat.StoreIncompatibleError, lock.LockTimeout, lock.RebaseGuard) as exc:
        warn(str(exc))
PY
		then
			echo "reviewbot-ensure-tickets: convergence failed or deferred; preserving local HEAD ${LOCAL_HEAD:-unreadable}; continuing with ensure registry" >&2
		fi
	fi
fi

# Idempotently ensure the store marker, merge driver, and Git configuration.
"$PY" - "$DIR" <<'PY'
import sys

from rebar._store.ensures import run_ensures

tracker = sys.argv[1]
for outcome in run_ensures(tracker):
    print(
        f"reviewbot-ensure-tickets: ensure {outcome.id}: {outcome.status} ({outcome.detail})",
        file=sys.stderr,
    )
PY

echo "reviewbot-ensure-tickets: $DIR is a writable rebar store" >&2
