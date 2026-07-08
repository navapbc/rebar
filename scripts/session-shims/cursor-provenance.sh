#!/usr/bin/env bash
# rebar session-provenance capture — Cursor cloud agent (story 7656). Referenced by the
# `install` command in .cursor/environment.json (see docs/session-id-shims.md).
#
# Cursor's environment.json has NO `env` field and Cursor exposes no readable session-id env
# var (per https://cursor.com/docs/cloud-agent/setup and the environment.schema.json). The
# first-class way to set env vars is the dashboard **Secrets** tab (dashboard-managed,
# environment-scoped). This script is a best-effort SUPPLEMENT for the harness TAG only: a
# cloud-agent VM is EPHEMERAL and single-session (torn down after the run), so exporting
# AI_AGENT into the VM's shell profile is session-scoped here (the G3 not-in-profile caveat is
# about long-lived local machines, not throwaway VMs) — so a Cursor `rebar claim` records
# `claim_harness = cursor`. REBAR_SESSION_ID has no native Cursor source; set it via the
# Secrets tab if you want the session id recorded.
set -u

tag='export AI_AGENT=cursor'
for rc in "$HOME/.bashrc" "$HOME/.profile"; do
    [ -e "$rc" ] || : >"$rc" 2>/dev/null || continue
    grep -qF "$tag" "$rc" 2>/dev/null || printf '%s\n' "$tag" >>"$rc" 2>/dev/null || true
done

exit 0
