#!/bin/sh
# Provision the persistent ticket store and attested-gate code checkout, then exec
# the MCP server as PID 1. Provisioning runs asynchronously so the long tickets clone
# cannot miss readiness; reads report store_uninitialized until convergence. Failures
# remain visible in logs and --provision-only status without blocking container startup.
set -e

# Test-overridable path to the shared registry convergence helper.
MCP_ENSURE_SCRIPT="${MCP_ENSURE_SCRIPT:-/app/infra/scripts/reviewbot-ensure-tickets.sh}"

# Attested gates resolve refs in this real repository, not the image's .git-less source copy.
MCP_CODE_DIR="${MCP_CODE_DIR:-/var/gerrit/site/mcp-code}"

# Allow a peer's large single-branch clone to finish.
MCP_RECLONE_LOCK_WAIT="${MCP_RECLONE_LOCK_WAIT:-7200}"

# Reject empty, relative, or root paths before any recursive clear.
dir_is_safe() {
  case "${1:-}" in
    "" | "/") return 1 ;;
    /*) return 0 ;;
    *) return 1 ;;
  esac
}

# Strip trailing slashes without reducing a validated path to root.
normalize_dir() {
  dir="$1"
  while :; do
    case "$dir" in
      */) dir="${dir%/}" ;;
      *) break ;;
    esac
  done
  printf '%s' "$dir"
}

# Clear only a validated target, from inside it, so failed cd cannot widen the deletion.
clear_dir() {
  dir_is_safe "$1" || return 1
  dir="$(normalize_dir "$1")"
  [ -d "$dir" ] || return 0
  # Relative globs add an independent safety boundary around rm.
  (cd "$dir" && rm -rf ./.[!.]* ./* 2>/dev/null) || true
  return 0
}

tracker_dir_is_safe() {
  dir_is_safe "${REBAR_TRACKER_DIR:-}"
}

tracker_dir() {
  normalize_dir "${REBAR_TRACKER_DIR}"
}

clear_tracker_dir() {
  if ! tracker_dir_is_safe; then
    echo "mcp: refusing to clear the tickets store — REBAR_TRACKER_DIR is not a safe absolute path" >&2
    return 1
  fi
  clear_dir "${REBAR_TRACKER_DIR}"
}

# Every shared-volume write, including registry ensure, is serialized here because
# asynchronous provisioning, restart:always, and operator restarts can overlap outside
# the deploy mutex. Lock the directory's persistent inode: sibling files are container-local,
# while files inside the directory would be cleared. Calls are sequential and non-nested;
# fd 9 is always closed while the command's soft-failure status is preserved.
with_dir_lock() {
  wdl_dir="$1"
  wdl_label="$2"
  shift 2
  exec 9<"$wdl_dir"
  if ! flock -w "$MCP_RECLONE_LOCK_WAIT" 9; then
    echo "mcp: timed out waiting for the ${wdl_label} lock" >&2
    exec 9<&-
    return 1
  fi
  wdl_rc=0
  "$@" || wdl_rc=$?
  exec 9<&-
  return "$wdl_rc"
}

store_head_resolves() {
  git -C "${REBAR_TRACKER_DIR}" rev-parse --verify -q HEAD >/dev/null 2>&1
}

reclone_store() {
  # Validate before locking or touching the filesystem.
  if ! tracker_dir_is_safe; then
    echo "mcp: refusing to clear the tickets store — REBAR_TRACKER_DIR is not a safe absolute path" >&2
    return 1
  fi
  # Create the leaf because the lock is its inode; production mounts it as a volume.
  dir="$(tracker_dir)"
  mkdir -p "$dir" 2>/dev/null || true
  if [ ! -d "$dir" ]; then
    echo "mcp: tickets store directory is unavailable" >&2
    return 1
  fi
  # A peer may be clearing or cloning this shared volume.
  with_dir_lock "$dir" "tickets re-clone" reclone_store_locked
}

# Clear and clone while holding the tracker-directory lock.
reclone_store_locked() {
  # Recheck after acquiring the lock because a peer may have completed the clone.
  if store_head_resolves; then
    echo "mcp: tickets store was re-cloned by another container" >&2
    return 0
  fi
  reclone_rc=0
  if clear_tracker_dir; then
    git clone --single-branch --branch tickets "${MCP_TICKETS_URL}" "${REBAR_TRACKER_DIR}" || {
      echo "mcp: tickets store clone deferred (deploy canary)" >&2
      reclone_rc=1
    }
  else
    reclone_rc=1
  fi
  return "$reclone_rc"
}

# Attested gates resolve refs in an asynchronous, persistent, blobless main clone.
# Container promotion never waits for this checkout; gates fail honestly until it converges.
code_repo_present() {
  # A partial clone can leave .git with a dangling HEAD; require both signals.
  [ -e "${MCP_CODE_DIR}/.git" ] &&
    git -C "${MCP_CODE_DIR}" rev-parse --verify -q HEAD >/dev/null 2>&1
}

clone_code() {
  if ! dir_is_safe "${MCP_CODE_DIR:-}"; then
    echo "mcp: refusing to clear the code checkout — MCP_CODE_DIR is not a safe absolute path" >&2
    return 1
  fi
  code_dir="$(normalize_dir "${MCP_CODE_DIR}")"
  # Create only the leaf under an existing mount; an absent mount skips code provisioning.
  code_parent="$(dirname "$code_dir")"
  if [ ! -d "$code_parent" ]; then
    echo "mcp: code checkout skipped — its mount point ${code_parent} is absent" >&2
    return 0
  fi
  mkdir -p "$code_dir" 2>/dev/null || true
  if [ ! -d "$code_dir" ]; then
    echo "mcp: code checkout directory is unavailable" >&2
    return 1
  fi
  # Serialize blue-green and restart writers on the checkout inode.
  with_dir_lock "$code_dir" "code checkout" clone_code_locked
}

# Clear and clone while holding the checkout-directory lock.
clone_code_locked() {
  # Recheck after waiting for a peer.
  if code_repo_present; then
    echo "mcp: code checkout was cloned by another container" >&2
    return 0
  fi
  code_rc=0
  if clear_dir "$code_dir"; then
    git clone --filter=blob:none --single-branch --branch main \
      "${MCP_TICKETS_URL}" "$code_dir" || {
      echo "mcp: code checkout clone deferred (deploy canary)" >&2
      code_rc=1
    }
  else
    echo "mcp: refusing to clear the code checkout — MCP_CODE_DIR is not a safe absolute path" >&2
    code_rc=1
  fi
  return "$code_rc"
}

provision_code() {
  if code_repo_present; then
    return 0
  fi
  [ -e "${MCP_CODE_DIR}/.git" ] &&
    echo "mcp: unusable code checkout (no resolvable HEAD) — re-cloning" >&2
  clone_code
}

# Command seam for running registry convergence under the tracker lock.
ensure_store() {
  sh "${MCP_ENSURE_SCRIPT}" "${REBAR_TRACKER_DIR}"
}

provision_store() {
  rc=0
  if [ -n "${MCP_TICKETS_PAT:-}" ]; then
    # Keep the PAT expansion inside Git's URL-scoped helper; helper failure remains soft.
    # shellcheck disable=SC2016
    git config --global "credential.${MCP_TICKETS_URL}.helper" \
      '!f() { echo username=x-access-token; echo "password=$MCP_TICKETS_PAT"; }; f' || rc=1
    git config --global "credential.${MCP_TICKETS_URL}.useHttpPath" false || rc=1
    [ "$rc" -eq 0 ] ||
      echo "mcp: could not install the tickets credential helper" >&2
    # Re-clone partial stores whose .git exists but HEAD does not resolve.
    if ! store_head_resolves; then
      [ -e "${REBAR_TRACKER_DIR}/.git" ] &&
        echo "mcp: unusable tickets store (no resolvable HEAD) — re-cloning" >&2
      reclone_store || rc=1
    fi
  fi
  # The public code clone is independent of the optional tickets PAT and always attempted.
  provision_code || rc=1
  # Registry convergence writes identity and .env-id, so serialize it on any safe tracker
  # inode. Unsafe or absent fixture paths retain the helper's non-fatal no-op behavior.
  if tracker_dir_is_safe; then
    ensure_dir="$(tracker_dir)"
    mkdir -p "$ensure_dir" 2>/dev/null || true
  else
    ensure_dir=""
  fi
  if [ -n "$ensure_dir" ] && [ -d "$ensure_dir" ]; then
    with_dir_lock "$ensure_dir" "tickets ensure" ensure_store || {
      echo "mcp: tickets store ensure deferred (see logs)" >&2
      rc=1
    }
  else
    ensure_store || {
      echo "mcp: tickets store ensure deferred (see logs)" >&2
      rc=1
    }
  fi
  echo "mcp: tickets store provisioning finished" >&2
  return "$rc"
}

# Tests and operators may run provisioning synchronously; normal startup backgrounds it.
if [ "${1:-}" = "--provision-only" ]; then
  provision_store
  exit $?
fi

provision_store &
exec "$@"
