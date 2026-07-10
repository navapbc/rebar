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
EMAIL="${REVIEWBOT_GIT_USER_EMAIL:-rebar-review-bot@navateam.com}"
NAME="${REVIEWBOT_GIT_USER_NAME:-rebar-review-bot}"
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
