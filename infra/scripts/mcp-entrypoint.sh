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
# The volume is SHARED across containers and the clone is NOT covered by the deploy mutex.
# Because provisioning is backgrounded (see the bottom of this file), the clone OUTLIVES the
# autodeploy tick that started it: /health answers before the store exists, so autodeploy
# declares success and releases its flock while the clone is still running, and the 2-minute
# timer can start a second container into the same volume. That one sees HEAD unresolvable --
# because the first is still cloning -- and would clear the directory out from under it. It
# is the poisoning mechanism this change exists to fix, not a hypothetical. Containers also
# start entirely outside autodeploy (`restart: always` on the compose service, an operator
# runbook restart), taking no flock at all. So serialization has to live HERE.
#
# The lock is a regular FILE created with `ln`, not a directory: `ln` fails atomically when
# the target exists, and the timestamp is written into a private temp BEFORE that temp is
# linked into place. So the lock name never appears without complete, readable content, and
# there is no window in which a peer reads a missing/empty marker and mistakes a
# freshly-taken lock for an abandoned one. Making acquisition atomic WITH its content is why
# the "missing marker" case needs no policy -- it cannot occur. (Treating a missing marker as
# FRESH would have closed that race by reopening a wedge: a container dying between the
# create and the write would make the lock permanently unbreakable -- the exact permanent-
# wedge failure this whole change exists to fix.) A timestamp that IS present but old still
# lets a lock abandoned by a container that died mid-clone be broken.
reclone_lock_path() { printf '%s' "$(tracker_dir).reclone.lock"; }

reclone_lock_is_stale() {
  lock="$1"
  # The current build's lock is a regular FILE holding the timestamp directly. A lock left by
  # the PREVIOUS build is a DIRECTORY with the timestamp in `acquired-at`; read both shapes so
  # an old-build peer's lock is still honoured (and still breakable) across the upgrade.
  if [ -d "$lock" ]; then
    started="$(cat "${lock}/acquired-at" 2>/dev/null || echo '')"
  else
    started="$(cat "$lock" 2>/dev/null || echo '')"
  fi
  now="$(date +%s)"
  case "$started" in
    '' | *[!0-9]*) return 0 ;; # unreadable/garbage marker — treat as abandoned
  esac
  [ "$((now - started))" -ge "$MCP_RECLONE_LOCK_STALE" ]
}

acquire_reclone_lock() {
  lock="$(reclone_lock_path)"
  tmp="${lock}.tmp.$$"
  waited=0
  while :; do
    # NEVER `ln` onto a DIRECTORY: `ln FILE DIR` SUCCEEDS by creating the link INSIDE DIR, so
    # a directory-shaped lock left by the previous build would be "acquired" by every
    # container at once -- serialization silently off, on exactly the upgrade path this store
    # is most fragile on. A directory here is a peer's old-build lock: skip the attempt and
    # let the staleness policy below decide whether to wait for it or break it.
    if [ ! -d "$lock" ]; then
      # Content complete BEFORE the lock name exists (see the note above).
      printf '%s\n' "$(date +%s)" > "$tmp" 2>/dev/null || {
        echo "mcp: cannot stage the tickets re-clone lock" >&2
        return 1
      }
      if ln "$tmp" "$lock" 2>/dev/null; then
        rm -f "$tmp"
        return 0
      fi
      rm -f "$tmp"
    fi
    if [ "$waited" -ge "$MCP_RECLONE_LOCK_WAIT" ]; then
      echo "mcp: timed out waiting for the tickets re-clone lock" >&2
      return 1
    fi
    if reclone_lock_is_stale "$lock"; then
      echo "mcp: breaking an abandoned tickets re-clone lock" >&2
      # `rm -f` cannot remove a directory, so fall back for an old-build lock.
      rm -f "$lock" 2>/dev/null || rm -rf "$lock" 2>/dev/null || true
    else
      sleep "$MCP_RECLONE_LOCK_POLL"
    fi
    # Advanced in BOTH branches: a lock that cannot be removed (a stale marker on a
    # read-only path) must exhaust the wait budget rather than spin forever.
    waited=$((waited + MCP_RECLONE_LOCK_POLL))
  done
}

release_reclone_lock() { rm -f "$(reclone_lock_path)" 2>/dev/null || true; }

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
