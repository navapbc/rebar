#!/usr/bin/env bash
# Runs rollback-bridge-cutover.sh inside a throwaway git worktree.
# The throwaway worktree is always removed on exit (trap EXIT).
# Usage: bash dryrun-bridge-rollback-in-worktree.sh
# Env:
#   DSO_DRYRUN_REPO_ROOT   — source repo root (default: git rev-parse --show-toplevel)
#   DSO_DRYRUN_CUTOVER_SHA — passed through to rollback-bridge-cutover.sh as
#                            DSO_ROLLBACK_CUTOVER_SHA (required)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${DSO_DRYRUN_REPO_ROOT:-$(git rev-parse --show-toplevel)}"
_ROLLBACK_SCRIPT="${DSO_DRYRUN_ROLLBACK_SCRIPT:-$SCRIPT_DIR/rollback-bridge-cutover.sh}"

if [[ -z "${DSO_DRYRUN_CUTOVER_SHA:-}" ]]; then
    echo "ERROR: DSO_DRYRUN_CUTOVER_SHA is required" >&2
    exit 2
fi

THROWAWAY="$(mktemp -d /tmp/dryrun-rollback.XXXXXX)"
git -C "$REPO_ROOT" worktree add "$THROWAWAY" HEAD

# Clean up the throwaway worktree AND any sibling test-artifacts directory
# created by the pre-commit-wrapper used inside the rollback commit step.
# The wrapper writes to /tmp/<basename>-test-artifacts-<basename>/ where
# <basename> is the throwaway worktree's mktemp basename — observed
# empirically during the live cfd6/7339 audit runs. Use a glob match
# scoped strictly under /tmp/ with the throwaway basename as the
# load-bearing prefix so the cleanup cannot collide with unrelated
# directories: any match MUST start with the unique mktemp basename
# ("dryrun-rollback.XXXXXX"), making cross-process collision impossible.
_THROWAWAY_BASENAME="$(basename "$THROWAWAY")"
_cleanup() {
    local _rc=$?
    git -C "$REPO_ROOT" worktree remove "$THROWAWAY" --force 2>/dev/null || true
    rm -rf "$THROWAWAY" 2>/dev/null || true
    # Guard the glob: only delete paths that BEGIN with our unique mktemp
    # basename. shopt nullglob so an empty match is a no-op (not literal '*').
    shopt -s nullglob
    local _artifact_dir
    for _artifact_dir in /tmp/"${_THROWAWAY_BASENAME}"-test-artifacts-*; do
        # Double-check before rm: must contain our basename prefix verbatim
        case "$_artifact_dir" in
            /tmp/"${_THROWAWAY_BASENAME}"-test-artifacts-*) rm -rf "$_artifact_dir" 2>/dev/null || true ;;
        esac
    done
    return "$_rc"
}
trap _cleanup EXIT

# SKIP_PUSH=1 keeps the dryrun fully local — no real push to origin and no
# gh run watch against the production branch.
DSO_ROLLBACK_REPO_ROOT="$THROWAWAY" \
DSO_ROLLBACK_CUTOVER_SHA="$DSO_DRYRUN_CUTOVER_SHA" \
DSO_ROLLBACK_SKIP_PUSH=1 \
bash "$_ROLLBACK_SCRIPT"

echo "DRYRUN OK: throwaway worktree cleaned up"
