#!/bin/sh
# reviewbot-ensure-tickets.sh — make a freshly-cloned `tickets` worktree writable by rebar.
#
# The review-bot persists code_review artifacts by cloning the shared `tickets` branch
# (git clone --single-branch --branch tickets) into $REVIEWBOT_TICKETS_DIR. A fresh
# single-branch clone is NOT yet a usable rebar store:
#
#   (a) it carries no repo-local git identity, and
#   (b) rebar's store marker `.env-id` is git-ignored, so the clone lacks it — and every
#       write then fails "ticket system not initialized" (see composer.py's `.env-id`
#       gate). emit_code_review_artifact (voter.py) swallows that failure best-effort, so
#       artifact emission becomes a SILENT no-op on every fresh clone
#       (bug desirous-judicial-hogget / d220).
#
# This script converges the clone into a writable store. It is IDEMPOTENT and safe to run
# on every container start — a no-op once the store is already converged.
#
# Contract (target dir = $1, else $REVIEWBOT_TICKETS_DIR):
#   * no-op + exit 0 when the dir is unset / absent / not a git clone yet (the entrypoint's
#     "clone deferred" deploy canary — there is simply no store to converge);
#   * set a repo-local git identity (user.email / user.name), overridable via
#     REVIEWBOT_GIT_USER_EMAIL / REVIEWBOT_GIT_USER_NAME;
#   * run rebar's idempotent ensure-registry against the dir so `.env-id` (+ the merge-ours
#     driver, gc config, gitattributes/gitignore) exist and writes succeed.
#
# The python interpreter is $REVIEWBOT_PYTHON (default `python3`) — overridable so a test
# harness can point it at a venv interpreter with rebar importable.
set -eu

DIR="${1:-${REVIEWBOT_TICKETS_DIR:-}}"
# Default to the Rebar Bot authorship identity's email (story 245e) so the review-bot's
# store writes attribute to identity 594c-9dcf-5ad6-4e6d via the git-email resolver.
# Overridable via REVIEWBOT_GIT_USER_EMAIL.
EMAIL="${REVIEWBOT_GIT_USER_EMAIL:-joeoakhart+bot@navapbc.com}"
NAME="${REVIEWBOT_GIT_USER_NAME:-Rebar Bot}"
PY="${REVIEWBOT_PYTHON:-python3}"

if [ -z "$DIR" ]; then
	echo "reviewbot-ensure-tickets: REVIEWBOT_TICKETS_DIR unset; nothing to do" >&2
	exit 0
fi

# The clone may be deferred (no PAT / offline at boot — the entrypoint's canary). A missing
# or non-git dir is not an error: there is no store to converge yet.
if [ ! -d "$DIR/.git" ]; then
	echo "reviewbot-ensure-tickets: $DIR is not a git clone yet (clone deferred); skipping" >&2
	exit 0
fi

# (a) Repo-local git identity. `git config` is idempotent (a no-op when already set to the
# same value); repo-local (not --global) so it is scoped to just this clone.
git -C "$DIR" config user.email "$EMAIL"
git -C "$DIR" config user.name "$NAME"

# (a2) ALSO set the identity globally (bug beb1). rebar resolves event attribution via
# `_seam.attribution_fields()`, which reads git config from `config.repo_root()` — inside this
# container that is /app (the rebar source clone), NOT $DIR. With no identity there,
# `author_email` came back empty and `resolve_current_identity()` returned None, so every event
# this bot wrote was stamped author "Unknown"/author_id null — and `_seam` gates signing on
# `if author_id and signing_key`, so a null author_id ALSO skipped signing entirely, before the
# key was ever consulted. 8880 events were written unsigned and unattributed this way.
# --global is correct HERE (unlike the repo-local line above), and the invariant that makes it
# correct is ONE CLONE PER CONTAINER -- not "this is the review bot". This script is now reused
# by the mcp container for its own ticket store (it takes the target dir as $1), which likewise
# holds exactly one clone, so there is still nothing for a global identity to leak into. State
# it that way so the next caller can check the property rather than infer it from the name.
git config --global user.email "$EMAIL"
git config --global user.name "$NAME"

# Reconcile this persistent single-branch clone before the bot starts writing artifacts.
# A normal compatible store uses rebar's non-destructive merge-as-union path.  The one
# deliberately narrow exception is the epoch reclaim migration: a clean pre-epoch clone
# may adopt the rewritten remote tip, but only when it had no local-only history before the
# fetch.  Keep the snapshots as commit ids; never reset to a moving tracking ref.
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
    # Compatible histories retain the normal union/recovery behavior.
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
    # Re-check every destructive precondition while holding rebar's unified write lock.
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
                    # ``pinned_remote`` is the immutable object id resolved immediately after
                    # fetch, not ``refs/remotes/origin/tickets``: a concurrent fetch may advance
                    # that tracking ref, but must neither change this adoption nor be overwritten.
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

# (b) Converge the rebar store: run the idempotent ensure-registry against the clone so the
# `.env-id` marker (the "initialized" gate that composer.py checks) and the merge-ours
# driver / gc config exist. run_ensures is check-then-act — a converged store makes zero
# commits — so this is safe to run on every boot.
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
