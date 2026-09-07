#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "::error::usage: install_sandbox_bwrap.sh <apt-package> [<apt-package> ...]" >&2
  exit 2
fi

apt_quiet() {
  local label=$1
  shift
  local output
  local rc=0
  output="$("$@" 2>&1)" || rc=$?
  if [ "$rc" -eq 0 ]; then
    return 0
  fi
  echo "::error::${label} failed with exit ${rc}; last output follows" >&2
  printf '%s\n' "$output" | tail -n 80 >&2
  return "$rc"
}

apt_quiet "apt-get update" apt-get update -qq
apt_quiet "apt-get install $*" apt-get install -y -qq "$@"
