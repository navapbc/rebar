#!/bin/sh
# mcp-entrypoint.sh — provision the rebar ticket store, then exec the MCP server.
#
# Installed onto PATH by infra/compose/Dockerfile.mcp (the same `install -m 0755` shape
# Dockerfile.reviewbot uses for reviewbot-ensure-tickets.sh) and wired as the image's
# ENTRYPOINT. It lives here, as a REAL FILE, rather than being echoed into existence
# inside a RUN block, so it is executable — and therefore testable — outside a container
# (tests/unit/test_mcp_entrypoint_provisioning.py runs `provision_store` against a temp
# dir with a stubbed `git`).
#
# Provision the store in the BACKGROUND and exec the server immediately.
# The `tickets` branch is ~200k commits; cloning it takes far longer than the
# 120s blue-green readiness deadline (autodeploy MCP_HEALTH_TIMEOUT). Doing it
# before exec meant the server never bound a port, /health never answered, and
# EVERY deploy was rolled back (bug unfit-beneficial-whimbrel). The review-bot
# entrypoint this was modelled on has no such readiness gate, which is why the
# same shape is safe there and not here.
#
# Degrading honestly while the clone runs is the read guard: tracker reads raise
# store_uninitialized until the store is converged, rather than reporting [] and
# passing an absent store off as an empty one.
#
# FAILURE POSTURE IS SOFT: no PAT, or a failed clone/ensure, must NOT stop the container
# booting. `provision_store` still RETURNS non-zero when a step failed so the failure
# surfaces (in the logs, and in the exit status under `--provision-only`) instead of
# being silently treated as success; because it is backgrounded, boot is unaffected.
set -e

# Overridable only so a test harness can point at a fixture; the default is the path the
# image installs (Dockerfile.reviewbot's shared ensure helper, copied in via COPY . /app).
MCP_ENSURE_SCRIPT="${MCP_ENSURE_SCRIPT:-/app/infra/scripts/reviewbot-ensure-tickets.sh}"

# Serialization knobs for the re-clone lock (seconds / minutes). Defaults are sized for a
# ~200k-commit single-branch clone.
MCP_RECLONE_LOCK_POLL="${MCP_RECLONE_LOCK_POLL:-10}"
MCP_RECLONE_LOCK_WAIT="${MCP_RECLONE_LOCK_WAIT:-7200}"
MCP_RECLONE_LOCK_STALE="${MCP_RECLONE_LOCK_STALE:-7200}"

# --- safety -----------------------------------------------------------------------
# The re-clone clears the directory with `rm -rf`. Expanding those globs against an empty
# `$REBAR_TRACKER_DIR` would mean `rm -rf /.[!.]* /*` — i.e. `rm -rf /*`. Refuse to run the
# clear at all unless the target is a non-empty ABSOLUTE path that is not `/`. (The clear
# itself adds a second, independent layer; see clear_tracker_dir.)
tracker_dir_is_safe() {
  case "${REBAR_TRACKER_DIR:-}" in
    "" | "/") return 1 ;;
    /*) return 0 ;;
    *) return 1 ;;
  esac
}

# The directory with any trailing slashes stripped, so the clear globs cannot degenerate.
tracker_dir() {
  dir="${REBAR_TRACKER_DIR}"
  while :; do
    case "$dir" in
      */) dir="${dir%/}" ;;
      *) break ;;
    esac
  done
  printf '%s' "$dir"
}

clear_tracker_dir() {
  if ! tracker_dir_is_safe; then
    echo "mcp: refusing to clear the tickets store — REBAR_TRACKER_DIR is not a safe absolute path" >&2
    return 1
  fi
  dir="$(tracker_dir)"
  [ -d "$dir" ] || return 0
  # Clear from INSIDE the directory using RELATIVE globs. Second, independent layer of the
  # same defence: if the target is ever wrong the `cd` simply fails and nothing is removed,
  # instead of `rm -rf "$dir"/*` expanding against `/`. The guard above should make this
  # unreachable; it is here so that a bypassed guard is a no-op rather than a catastrophe.
  (cd "$dir" && rm -rf ./.[!.]* ./* 2>/dev/null) || true
  return 0
}

# --- serialization ----------------------------------------------------------------
# The volume is SHARED across containers (blue/green overlap, a restart racing a rollout).
# Two containers can both see HEAD unresolvable, and an unserialized rm+clone interleaves:
# one clears the directory the other is mid-clone into. `mkdir` is atomic on the volume's
# filesystem, so it is the lock primitive; a timestamp inside it lets a lock abandoned by a
# container that died mid-clone be broken rather than wedging the store forever.
reclone_lock_path() { printf '%s' "$(tracker_dir).reclone.lock"; }

reclone_lock_is_stale() {
  lock="$1"
  started="$(cat "${lock}/acquired-at" 2>/dev/null || echo 0)"
  now="$(date +%s)"
  case "$started" in
    '' | *[!0-9]*) return 0 ;; # unreadable/garbage marker — treat as abandoned
  esac
  [ "$((now - started))" -ge "$MCP_RECLONE_LOCK_STALE" ]
}

acquire_reclone_lock() {
  lock="$(reclone_lock_path)"
  waited=0
  while ! mkdir "$lock" 2>/dev/null; do
    if reclone_lock_is_stale "$lock"; then
      echo "mcp: breaking an abandoned tickets re-clone lock" >&2
      rm -rf "$lock" 2>/dev/null || true
      continue
    fi
    if [ "$waited" -ge "$MCP_RECLONE_LOCK_WAIT" ]; then
      echo "mcp: timed out waiting for the tickets re-clone lock" >&2
      return 1
    fi
    sleep "$MCP_RECLONE_LOCK_POLL"
    waited=$((waited + MCP_RECLONE_LOCK_POLL))
  done
  date +%s > "${lock}/acquired-at" 2>/dev/null || true
  return 0
}

release_reclone_lock() { rm -rf "$(reclone_lock_path)" 2>/dev/null || true; }

store_head_resolves() {
  git -C "${REBAR_TRACKER_DIR}" rev-parse --verify -q HEAD >/dev/null 2>&1
}

# --- provisioning -----------------------------------------------------------------
reclone_store() {
  # Validate the target BEFORE taking a lock or touching the filesystem.
  if ! tracker_dir_is_safe; then
    echo "mcp: refusing to clear the tickets store — REBAR_TRACKER_DIR is not a safe absolute path" >&2
    return 1
  fi
  # Serialized: another container may already be clearing/cloning this same volume.
  acquire_reclone_lock || return 1
  # Double-checked: the container that held the lock may have finished the clone while we
  # waited, in which case re-cloning ~200k commits again would be pure waste.
  if store_head_resolves; then
    release_reclone_lock
    echo "mcp: tickets store was re-cloned by another container" >&2
    return 0
  fi
  rc=0
  if clear_tracker_dir; then
    git clone --single-branch --branch tickets "${MCP_TICKETS_URL}" "${REBAR_TRACKER_DIR}" || {
      echo "mcp: tickets store clone deferred (deploy canary)" >&2
      rc=1
    }
  else
    rc=1
  fi
  release_reclone_lock
  return "$rc"
}

provision_store() {
  rc=0
  if [ -n "${MCP_TICKETS_PAT:-}" ]; then
    # SC2016 is the POINT: the helper body must reach git UNEXPANDED, so the PAT is read
    # from the environment when git invokes it and never lands in ~/.gitconfig on disk.
    # shellcheck disable=SC2016
    git config --global "credential.${MCP_TICKETS_URL}.helper" \
      '!f() { echo username=x-access-token; echo "password=$MCP_TICKETS_PAT"; }; f'
    git config --global "credential.${MCP_TICKETS_URL}.useHttpPath" false
    # A `.git` directory is NOT proof of a usable clone. A container removed mid-clone
    # (which is exactly what the 120s health rollback did, repeatedly) leaves a PERSISTENT
    # volume holding orphaned objects, an empty refs/heads and HEAD dangling at
    # refs/heads/.invalid. Skipping on mere existence made that state permanent: every
    # later container inherited the poisoned volume, served an empty tracker, and never
    # re-cloned. Require a resolvable HEAD.
    if ! store_head_resolves; then
      [ -e "${REBAR_TRACKER_DIR}/.git" ] &&
        echo "mcp: unusable tickets store (no resolvable HEAD) — re-cloning" >&2
      reclone_store || rc=1
    fi
  fi
  # Converge the (possibly freshly-cloned) worktree into a WRITABLE rebar store:
  # repo-local git identity + the `.env-id` marker. Idempotent.
  sh "${MCP_ENSURE_SCRIPT}" "${REBAR_TRACKER_DIR}" || {
    echo "mcp: tickets store ensure deferred (see logs)" >&2
    rc=1
  }
  echo "mcp: tickets store provisioning finished" >&2
  return "$rc"
}

# Run the provisioning step alone, synchronously, and report its status. Used by the tests
# (and available to an operator debugging a volume) — the container path below is unchanged.
if [ "${1:-}" = "--provision-only" ]; then
  provision_store
  exit $?
fi

provision_store &
exec "$@"
