#!/bin/sh
# mcp-entrypoint.sh — provision the rebar ticket store + the code checkout the attested
# LLM gates resolve refs against, then exec the MCP server.
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

# Where the CODE checkout the attested gates resolve refs against lives. `.dockerignore`
# excludes `.git` and Dockerfile.mcp is `COPY . /app`, so the image holds a SOURCE COPY with
# no object database at all: with REBAR_ROOT unset the repo root resolved to the WORKDIR
# (/app) and EVERY attested-source gate died at
# `cannot resolve ref 'origin/main' to a commit in '.'` — no plan-review or
# completion-verifier attestation could be earned through the deployed server, so nothing
# could be claimed or closed through it. (`source: "local"` still ran, which is what pinned
# the fault on the missing REPOSITORY rather than on the gate code.) ADR 0104 §3 designs the
# server to mint both op-cert kinds; this wires the checkout that design requires.
# Overridable so a test harness can point it at a fixture; the default is the mounted volume.
MCP_CODE_DIR="${MCP_CODE_DIR:-/var/gerrit/site/mcp-code}"

# How long to wait for a peer's re-clone before giving up, in seconds. Sized for a
# ~200k-commit single-branch clone.
MCP_RECLONE_LOCK_WAIT="${MCP_RECLONE_LOCK_WAIT:-7200}"

# --- safety -----------------------------------------------------------------------
# The re-clone clears the directory with `rm -rf`. Expanding those globs against an empty
# `$REBAR_TRACKER_DIR` would mean `rm -rf /.[!.]* /*` — i.e. `rm -rf /*`. Refuse to run the
# clear at all unless the target is a non-empty ABSOLUTE path that is not `/`. (The clear
# itself adds a second, independent layer; see clear_tracker_dir.)
dir_is_safe() {
  case "${1:-}" in
    "" | "/") return 1 ;;
    /*) return 0 ;;
    *) return 1 ;;
  esac
}

# The directory with any trailing slashes stripped, so the clear globs cannot degenerate.
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

# Empty a directory that a clone is about to target. BOTH layers of the defence live here,
# so every caller (tickets store, code checkout) inherits them; there is no path that clears
# a directory without them.
clear_dir() {
  dir_is_safe "$1" || return 1
  dir="$(normalize_dir "$1")"
  [ -d "$dir" ] || return 0
  # Clear from INSIDE the directory using RELATIVE globs. Second, independent layer of the
  # same defence: if the target is ever wrong the `cd` simply fails and nothing is removed,
  # instead of `rm -rf "$dir"/*` expanding against `/`. The guard above should make this
  # unreachable; it is here so that a bypassed guard is a no-op rather than a catastrophe.
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
# The lock is an `flock` on the TRACKER DIRECTORY'S OWN INODE -- the same primitive, and the
# same fd-9 shape, autodeploy.sh uses for the deploy lock. The inode matters:
#
#   * A lock file BESIDE the directory (`${dir}.reclone.lock`) does not work. The volume is
#     mounted AT `$REBAR_TRACKER_DIR`, so a SIBLING path is not on the volume at all -- it
#     lands in each container's own overlay filesystem. Every container would get a private
#     lock and serialization would be silently INERT.
#   * A lock file INSIDE the directory does not work either: `clear_tracker_dir` wipes the
#     contents and `git clone` demands an empty target, so the lock would be destroyed by the
#     very operation it guards, or would block it.
#
# The directory itself is never removed, so the inode survives the clear, and it is not a
# directory ENTRY, so it cannot make the clone target non-empty. Being a kernel lock, it is
# released automatically when the holder dies -- so a container killed mid-clone cannot wedge
# the store, and there is no stale-lock detection, timestamp, or marker file to get wrong.

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
  # The lock IS the directory's inode, so it has to exist before it can be opened. On the box
  # it always does -- it is the volume's mount point -- but a fresh fixture may not have it.
  dir="$(tracker_dir)"
  mkdir -p "$dir" 2>/dev/null || true
  if [ ! -d "$dir" ]; then
    echo "mcp: tickets store directory is unavailable" >&2
    return 1
  fi
  # Serialized: another container may already be clearing/cloning this same volume.
  exec 9<"$dir"
  if ! flock -w "$MCP_RECLONE_LOCK_WAIT" 9; then
    echo "mcp: timed out waiting for the tickets re-clone lock" >&2
    exec 9<&-
    return 1
  fi
  # Double-checked: the container that held the lock may have finished the clone while we
  # waited, in which case re-cloning ~200k commits again would be pure waste.
  if store_head_resolves; then
    exec 9<&-
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
  exec 9<&-
  return "$rc"
}

# --- code checkout ------------------------------------------------------------------
# The attested gates (review_plan / verify_completion, `source=attested`) pin a snapshot at a
# ref — `origin/main` by default — which means they need an OBJECT DATABASE to resolve it
# against. Provide one: a blobless, single-branch clone of `main` from the SAME repository
# URL the tickets store clones from, on its own persistent volume, with REBAR_ROOT pointed at
# it by the compose service / autodeploy's `docker run`.
#
# BLOBLESS (`--filter=blob:none`) is not an optimisation detail: a full clone of this
# repository is prohibitively large for the box, and the snapshot machinery already fetches
# the objects it needs on demand, so the partial clone is the shape that fits.
#
# Like the tickets clone this runs in the BACKGROUND (see the bottom of the file) and is
# therefore allowed to outlive the 120s blue-green readiness deadline. Until it converges the
# gates fail exactly as they do today — no worse — and NOTHING gates container promotion on
# it: gating promotion on a backgrounded clone is precisely what produced the rollback loop
# in bug unfit-beneficial-whimbrel.
code_repo_present() {
  # `.git` FIRST, and per-directory: a resolvable HEAD elsewhere says nothing about this
  # checkout. A container removed mid-clone leaves `.git` behind with HEAD dangling, so the
  # marker alone is not proof either — require both, exactly as the tickets store does.
  [ -e "${MCP_CODE_DIR}/.git" ] &&
    git -C "${MCP_CODE_DIR}" rev-parse --verify -q HEAD >/dev/null 2>&1
}

clone_code() {
  if ! dir_is_safe "${MCP_CODE_DIR:-}"; then
    echo "mcp: refusing to clear the code checkout — MCP_CODE_DIR is not a safe absolute path" >&2
    return 1
  fi
  code_dir="$(normalize_dir "${MCP_CODE_DIR}")"
  # Only ever create the LEAF. The checkout lives on a mounted volume whose parent exists on
  # the box; refusing to conjure the whole path keeps a mis-set MCP_CODE_DIR from scattering a
  # clone across the filesystem. An absent mount point means this host simply has nowhere to
  # put a checkout (a bare `docker run` of the image, a harness driving --provision-only for
  # the tickets store) — that is NOT a provisioning failure, so it is announced and SKIPPED
  # rather than reported as one. A failure of the clone ITSELF still surfaces, below.
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
  # Serialized on the checkout directory's OWN INODE, for the same reasons the tickets
  # re-clone is (shared volume, blue/green overlap, `restart: always` containers that take no
  # deploy lock). Separate fd from the tickets lock so the two never alias.
  exec 8<"$code_dir"
  if ! flock -w "$MCP_RECLONE_LOCK_WAIT" 8; then
    echo "mcp: timed out waiting for the code checkout lock" >&2
    exec 8<&-
    return 1
  fi
  # Double-checked: a peer may have finished the clone while we waited.
  if code_repo_present; then
    exec 8<&-
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
  exec 8<&-
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

provision_store() {
  rc=0
  if [ -n "${MCP_TICKETS_PAT:-}" ]; then
    # SC2016 is the POINT: the helper body must reach git UNEXPANDED, so the PAT is read
    # from the environment when git invokes it and never lands in ~/.gitconfig on disk.
    # Guarded: these are simple commands under `set -e`, so an unguarded failure would abort
    # provision_store BEFORE the ensure step and before the terminal log line -- turning a
    # credential-helper problem into a silent, statusless exit. Soft posture: record it and
    # carry on; the clone below will surface its own failure.
    # shellcheck disable=SC2016
    git config --global "credential.${MCP_TICKETS_URL}.helper" \
      '!f() { echo username=x-access-token; echo "password=$MCP_TICKETS_PAT"; }; f' || rc=1
    git config --global "credential.${MCP_TICKETS_URL}.useHttpPath" false || rc=1
    [ "$rc" -eq 0 ] ||
      echo "mcp: could not install the tickets credential helper" >&2
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
  # The code checkout the attested gates resolve refs against. DELIBERATELY OUTSIDE the PAT
  # guard above, and it must stay there: `navapbc/rebar` answers anonymous git requests
  # (`GET .../info/refs?service=git-upload-pack` returns 200), so this clone needs NO
  # credential — whereas REBAR_ROOT is exported UNCONDITIONALLY (Dockerfile.mcp ENV,
  # docker-compose.yml, autodeploy's `docker run`) and a blank MCP_TICKETS_PAT is a SUPPORTED
  # state. Nesting this under the PAT guard therefore points REBAR_ROOT at a directory that was
  # never created, and every attested gate dies exactly as it did before this checkout existed.
  # Same soft posture as everything else here: a failure is REPORTED and stepped over, never
  # swallowed into a success and never allowed to stop the container booting.
  provision_code || rc=1
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
