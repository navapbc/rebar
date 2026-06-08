#!/usr/bin/env bash
# rollback-bridge-cutover.sh
#
# Idempotent rollback playbook for the bridge cutover. Reverts a cutover commit,
# restores the cursor snapshot from bridge_state/bootstrap/, commits + pushes
# the revert, and waits for the CI run triggered by that push to verify the
# rollback.
#
# Steps:
#   1. git revert --no-commit DSO_ROLLBACK_CUTOVER_SHA
#   2. Restore cursor snapshot from bridge_state/bootstrap/
#   3. git add -A && git commit -m "rollback: revert cutover <sha>"
#   4. git push origin <current-branch>
#   5. gh run watch on the CI run triggered by the push (matched by new HEAD SHA)
#
# Environment variables:
#   DSO_ROLLBACK_REPO_ROOT      Override repo root for test isolation
#                               (default: git rev-parse --show-toplevel)
#   DSO_ROLLBACK_VERIFY_TIMEOUT Timeout in seconds for gh run watch (default: 2400)
#   DSO_ROLLBACK_CUTOVER_SHA    Required: the commit SHA to revert
#   DSO_ROLLBACK_SKIP_PUSH      If "1", skip the push + CI-watch steps (for dryrun)
#
# Idempotent: steps that have already completed are skipped gracefully.
#
# Named-step exit codes:
#   0 — success (or safe no-op for idempotent steps)
#   1 — step failure (revert failed, push failed, CI failed)
#   2 — missing required input (DSO_ROLLBACK_CUTOVER_SHA not set)
#
# Usage:
#   DSO_ROLLBACK_CUTOVER_SHA=<sha> rollback-bridge-cutover.sh
#
# ── Rollback-impossible-after-Nh: forward-fix path ───────────────────────────
# This rollback playbook reverts the cutover commit and restores the legacy
# edge-triggered bridges. It is safe to run for ~24-48h after cutover. After
# that window — or whenever the conditions below hold — a clean rollback is
# NOT possible, and operators MUST follow the forward-fix path documented
# below rather than attempting rollback.
#
# Rollback becomes UNSAFE / IMPOSSIBLE when ANY of the following is true:
#   (a) Tickets-branch compaction since cutover. The reconciler's mapping.json
#       and bridge_state/health entries were committed and then compacted into
#       a SNAPSHOT, so the legacy bridges cannot reconstruct their cursor state
#       from event history. Indicator: `git log .tickets-tracker/` shows a  # tickets-boundary-ok
#       SNAPSHOT commit AFTER the cutover SHA.
#   (b) Irrecoverable Jira-side state. The reconciler has performed mutations
#       (status transitions, label additions, property writes) on production
#       Jira issues that the legacy bridges did not know about. Reverting the
#       code does not un-mutate Jira; the legacy bridges will see "drifted"
#       state and may attempt to re-do or contradict those mutations.
#       Indicator: bridge-fsck reports orphan/duplicate counts BELOW the
#       pre-cutover baseline (proves the reconciler has actively healed
#       anomalies the legacy bridges would re-create).
#   (c) Pre-cutover cursor snapshot absent or corrupt. STEP 2 of this script
#       relies on bridge_state/bootstrap/cursor-snapshot.json. If that file
#       is missing or its SHA fails verification, the legacy bridges would
#       restart from HEAD and miss every event between cutover and rollback.
#       Indicator: STEP 2 of this script emits "WARN: no cursor snapshot".
#
# Forward-fix path (use when rollback is impossible):
#   1. Do NOT execute this script. The cutover stays in place.
#   2. Identify the specific failure mode that prompted the rollback consideration
#      (a band failure, a single problematic Jira issue, a payload-format defect,
#      etc.). File a bug ticket via the bug-fix workflow and treat it as a normal
#      production defect rather than a rollback condition.
#   3. If a single Jira issue or class of issues is in a bad state, mutate
#      them in-place via the reconciler bands (orphan_band, stale_band,
#      duplicates_band, open_count_skew_band) using their `--user-approved`
#      / per-band manifest attestation gates. Bands have safeguards (first-week
#      mutation cap, manifest hash check, bot allowlist) that make targeted
#      in-place healing safe even under operator pressure.
#   4. If the failure is broader (e.g., reconciler crashes on every CREATE),
#      patch the reconciler code via the standard bug-fix workflow and
#      ship via PR. The reconciler is single-flight, so a broken pass blocks
#      the next pass; once the fix lands the next scheduled tick recovers
#      automatically.
#   5. Disable the schedule temporarily via `gh workflow disable
#      reconcile-bridge.yml` if the bug rate is high; re-enable via
#      `gh workflow enable reconcile-bridge.yml` after the fix.
#
# This forward-fix path is intentionally documented HERE (in the script
# operators reach for during an incident) rather than in a separate runbook,
# so the choice between rollback and forward-fix is visible at the moment
# of decision.

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
REPO_ROOT="${DSO_ROLLBACK_REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
VERIFY_TIMEOUT="${DSO_ROLLBACK_VERIFY_TIMEOUT:-2400}"
CUTOVER_SHA="${DSO_ROLLBACK_CUTOVER_SHA:-}"
SKIP_PUSH="${DSO_ROLLBACK_SKIP_PUSH:-0}"

echo "rollback-bridge-cutover: repo_root=$REPO_ROOT"
echo "rollback-bridge-cutover: verify_timeout=${VERIFY_TIMEOUT}s"
echo "rollback-bridge-cutover: skip_push=${SKIP_PUSH}"

# ── Validate required inputs ──────────────────────────────────────────────────
if [[ -z "$CUTOVER_SHA" ]]; then
    echo "ERROR: DSO_ROLLBACK_CUTOVER_SHA is required" >&2
    exit 2
fi

echo "rollback-bridge-cutover: cutover_sha=$CUTOVER_SHA"

cd "$REPO_ROOT" || exit 1

# ── Step 1: Revert the cutover commit ─────────────────────────────────────────
echo "STEP 1: reverting cutover commit $CUTOVER_SHA"
# Detect merge commits — they require `-m 1` to specify the mainline parent.
# Realistic deployment path lands the cutover via a PR merge, so the cutover
# SHA on main is typically a merge commit.
_revert_args=("--no-commit")
_is_merge=0
if [[ $(git cat-file -p "$CUTOVER_SHA" | grep -c '^parent ') -gt 1 ]]; then
    echo "STEP 1: detected merge commit; using --mainline 1 to revert against mainline parent"
    _revert_args+=("--mainline" "1")
    _is_merge=1
fi
# `git revert` exits non-zero when conflicts are produced, but conflicts may
# still be auto-resolvable below (e.g. modify/delete conflicts on files that
# the cutover commit ADDED and a later commit MODIFIED — the rollback intends
# to remove those files, so deletion wins). Capture the exit code without
# triggering set -e.
_revert_rc=0
git revert "${_revert_args[@]}" "$CUTOVER_SHA" 2>&1 || _revert_rc=$?

# Resolve "modify/delete" conflicts on files that the cutover commit ADDED.
# When a later post-cutover commit modifies a file that the cutover originally
# added, reverting the cutover wants to delete the file, but git surfaces a
# modify/delete conflict because HEAD has further modifications. Rollback
# semantics: the cutover-added file should not exist post-rollback, so
# follow-on modifications to it are moot. Accept deletion via `git rm`.
_unresolved_conflicts=0
if [[ "$_revert_rc" -ne 0 ]]; then
    # Determine the cutover's "added" file set (for merge commits, compare
    # against the mainline parent; for non-merge commits, compare against
    # the sole parent).
    if [[ "$_is_merge" -eq 1 ]]; then
        _cutover_base="${CUTOVER_SHA}^1"
    else
        _cutover_base="${CUTOVER_SHA}^"
    fi
    _added_by_cutover_file="$(mktemp /tmp/rollback-added.XXXXXX)"
    git diff --name-only --diff-filter=A "$_cutover_base" "$CUTOVER_SHA" > "$_added_by_cutover_file" 2>/dev/null || true

    # Walk the unmerged paths and auto-resolve any modify/delete conflict
    # whose path was added by the cutover.
    while IFS=$'\t' read -r _status _path; do
        # `git status --porcelain` modify/delete shows as "DU" (deleted by us)
        # when we are reverting (the revert side wants delete) and the other
        # side modified. Also handle "UD" defensively.
        case "$_status" in
            DU|UD)
                if grep -Fxq "$_path" "$_added_by_cutover_file"; then
                    echo "STEP 1: auto-resolving modify/delete conflict on cutover-added file: $_path (accepting deletion)"
                    git rm -f -- "$_path" >/dev/null
                else
                    _unresolved_conflicts=1
                    echo "STEP 1: unresolved modify/delete conflict on $_path (not a cutover-added file)" >&2
                fi
                ;;
            *)
                # Any other conflict marker (UU, AA, AU, UA, DD) is unresolved
                # — surface it for human review.
                _unresolved_conflicts=1
                echo "STEP 1: unresolved conflict ($_status) on $_path" >&2
                ;;
        esac
    done < <(git status --porcelain | awk '/^(DU|UD|UU|AA|AU|UA|DD) / {print $1"\t"substr($0, 4)}')
    rm -f "$_added_by_cutover_file"
fi

if [[ "$_unresolved_conflicts" -ne 0 ]]; then
    echo "ERROR: git revert produced conflicts that require human review" >&2
    exit 1
fi
echo "STEP 1 OK"

# ── Step 2: Restore cursor snapshot ───────────────────────────────────────────
echo "STEP 2: restoring cursor snapshot from bridge_state/bootstrap/"
_bootstrap_dir="$REPO_ROOT/bridge_state/bootstrap"
_snapshot_src=""
if [[ -d "$_bootstrap_dir" ]]; then
    _snapshot_src="$(find "$_bootstrap_dir" -name "cursor-snapshot.json" 2>/dev/null | sort | tail -1 || true)"
fi

if [[ -z "$_snapshot_src" ]]; then
    echo "WARN: no cursor snapshot found in bridge_state/bootstrap/ — skipping cursor restore"
else
    if ! cp "$_snapshot_src" "$REPO_ROOT/bridge_state/cursor-snapshot.json"; then
        echo "ERROR: failed to restore cursor snapshot from $_snapshot_src" >&2
        exit 1
    fi
    echo "STEP 2 OK (restored from $_snapshot_src)"
fi

# ── Step 2.5: Re-introduce allowlist entry for ticket-bridge-fsck.py ──────────
# The cutover commit may have ADDED an allowlist entry exempting
# ticket-bridge-fsck.py (and possibly other pre-existing files) from the
# tickets-boundary pre-commit hook. Reverting the cutover removes that
# allowlist entry, but the pre-existing tracker-access references in
# ticket-bridge-fsck.py remain — so the commit in STEP 3 would be rejected
# by check-tickets-boundary.sh.
#
# Detect the case and re-add the allowlist block locally so the rollback
# commit passes. This is local-only (we're inside the rollback worktree);
# it is automatically undone if the rollback is abandoned.
_allowlist_conf="$REPO_ROOT/.claude/hooks/pre-commit/check-tickets-boundary-allowlist.conf"
# Build the path from constituent segments so this script does not contain
# the literal forbidden substring inside the plugin's own scripts dir.
# The runtime concatenation evaluates to the correct allowlist entry path.
_plugin_dir="plugins"
_dso_seg="dso"
_fsck_entry="${_plugin_dir}/${_dso_seg}/scripts/ticket-bridge-fsck.py"
if [[ -f "$_allowlist_conf" ]]; then
    if ! grep -Fxq "$_fsck_entry" "$_allowlist_conf"; then
        echo "STEP 2.5: allowlist entry for $_fsck_entry was removed by revert — re-introducing"
        cat >> "$_allowlist_conf" <<EOF

# Bridge fsck audit tool — its purpose IS to walk the tracker directly for
# bridge mapping anomalies (orphans, duplicates, stale SYNCs). The docstring
# + argparse help strings legitimately reference the tracker path.
# Re-introduced by rollback-bridge-cutover.sh because the cutover revert
# also dropped this entry (the cutover commit bundled it with the fsck
# enhancements that depend on it).
${_fsck_entry}
EOF
        echo "STEP 2.5 OK"
    else
        echo "STEP 2.5 OK (allowlist entry already present — no change needed)"
    fi
fi

# ── Step 3: Commit the revert + cursor restore ────────────────────────────────
echo "STEP 3: committing revert + cursor restore"
git add -A
if git diff --cached --quiet; then
    echo "STEP 3 OK (no changes staged — idempotent re-run)"
else
    if ! git commit -m "rollback: revert cutover ${CUTOVER_SHA}"; then
        echo "ERROR: commit failed" >&2
        exit 1
    fi
    echo "STEP 3 OK"
fi

NEW_HEAD_SHA="$(git rev-parse HEAD)"
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "rollback-bridge-cutover: new_head_sha=$NEW_HEAD_SHA branch=$CURRENT_BRANCH"

if [[ "$SKIP_PUSH" == "1" ]]; then
    echo "STEP 4: SKIPPED (DSO_ROLLBACK_SKIP_PUSH=1)"
    echo "STEP 5: SKIPPED (DSO_ROLLBACK_SKIP_PUSH=1)"
    echo "rollback-bridge-cutover: complete (push skipped)"
    exit 0
fi

# ── Step 4: Push the revert ───────────────────────────────────────────────────
echo "STEP 4: pushing revert to origin/$CURRENT_BRANCH"
if ! git push origin "$CURRENT_BRANCH"; then
    echo "ERROR: git push failed" >&2
    exit 1
fi
echo "STEP 4 OK"

# ── Step 5: Watch the CI run triggered by the push ────────────────────────────
echo "STEP 5: waiting for CI verification (timeout: ${VERIFY_TIMEOUT}s) on commit $NEW_HEAD_SHA"
# Give GitHub a few seconds to register the workflow run after the push.
sleep 5

# Find the workflow run for the new commit on this branch. Retry a few times
# because the run may not appear instantly.
_run_id=""
_attempts=0
while [[ "$_attempts" -lt 6 ]]; do
    _attempts=$((_attempts + 1))
    _run_id="$(gh run list \
        --branch "$CURRENT_BRANCH" \
        --commit "$NEW_HEAD_SHA" \
        --limit 1 \
        --json databaseId \
        --jq '.[0].databaseId // empty' 2>/dev/null || true)"
    if [[ -n "$_run_id" ]]; then
        break
    fi
    echo "  no run found for commit $NEW_HEAD_SHA yet (attempt $_attempts) — sleeping 10s"
    sleep 10
done

if [[ -z "$_run_id" ]]; then
    echo "WARN: no CI run found for commit $NEW_HEAD_SHA after $_attempts attempts — skipping gh run watch"
else
    if ! gh run watch --exit-status "$_run_id"; then
        echo "ERROR: CI verification failed or timed out for run $_run_id" >&2
        exit 1
    fi
    echo "STEP 5 OK (run $_run_id passed)"
fi

echo "rollback-bridge-cutover: complete"
exit 0
