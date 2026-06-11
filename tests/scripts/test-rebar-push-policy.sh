#!/usr/bin/env bash
# REBAR_PUSH auto-push policy (ticket hip-rod-graze, risk R9).
#
# Every write funnels its auto-push through _push_tickets_branch, which honours
# REBAR_PUSH=always|async|off (default always). This pins all three modes against
# a real (local bare) origin: off never pushes, always pushes synchronously before
# the write returns, async returns immediately and the push lands shortly after.
set -euo pipefail

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
git init -q --bare "$tmp/origin.git"
git init -q "$tmp/work"
cd "$tmp/work"
git config user.email t@t.co
git config user.name t
git remote add origin "$tmp/origin.git"
export REBAR_ROOT="$tmp/work" PROJECT_ROOT="$tmp/work"
rebar init --silent >/dev/null 2>&1

origin_ref() { git --git-dir="$tmp/origin.git" rev-parse refs/heads/tickets 2>/dev/null || echo NONE; }

fail=0

# ── off: origin must not move ────────────────────────────────────────────────
before=$(origin_ref)
REBAR_PUSH=off rebar create task "off" >/dev/null 2>&1
if [ "$(origin_ref)" = "$before" ]; then
    echo "PASS: REBAR_PUSH=off did not push"
else
    echo "FAIL: REBAR_PUSH=off pushed to origin"; fail=1
fi

# ── always: origin advances synchronously, before the command returns ────────
before=$(origin_ref)
REBAR_PUSH=always rebar create task "always" >/dev/null 2>&1
if [ "$(origin_ref)" != "$before" ]; then
    echo "PASS: REBAR_PUSH=always pushed synchronously"
else
    echo "FAIL: REBAR_PUSH=always did not push"; fail=1
fi

# ── async: returns immediately; the push lands within a short bounded wait ────
before=$(origin_ref)
REBAR_PUSH=async rebar create task "async" >/dev/null 2>&1
after="$before"
for _ in $(seq 1 25); do
    after=$(origin_ref)
    [ "$after" != "$before" ] && break
    sleep 0.4
done
if [ "$after" != "$before" ]; then
    echo "PASS: REBAR_PUSH=async pushed in the background"
else
    echo "FAIL: REBAR_PUSH=async never pushed"; fail=1
fi

# Case/space-insensitive parse (e.g. ' OFF ') must also disable the push.
before=$(origin_ref)
REBAR_PUSH=" OFF " rebar create task "off2" >/dev/null 2>&1
if [ "$(origin_ref)" = "$before" ]; then
    echo "PASS: REBAR_PUSH=' OFF ' (case/space-insensitive) did not push"
else
    echo "FAIL: REBAR_PUSH=' OFF ' pushed"; fail=1
fi

exit "$fail"
