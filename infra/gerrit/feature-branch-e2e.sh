#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# feature-branch-e2e.sh — the committed LIVE 7-scenario verification artifact for
# the epic-88ab "server-side feature branch + reviewed merge change" pattern
# (story leafy-vogue-ingot / S5). Sibling to reviewbot-e2e.sh; where that script
# proves the SINGLE-change happy path, this one proves the FEATURE-BRANCH flow —
# stories reviewed INTO refs/heads/feature/<name>, then landed via one --no-ff
# merge change to main — under the seven scenarios the story enumerates.
#
# It PROVES the flow against the RUNNING Gerrit (rebar.solutions.navateam.com) +
# the deployed review bot + CI on the navapbc/rebar GitHub Actions mirror — not
# offline unit tests. Every scenario records the live change number(s) and the two
# votes it observed (LLM-Review from the review bot, Verified from the CI bot).
#
# DRIVE MODEL — HTTPS/REST (not the admin SSH key). All Gerrit ops go over HTTPS as
# an operator who is BOTH an Administrator AND a feature-branch-drivers member:
#   - push story change:  git push https://<user>@host/a/rebar HEAD:refs/for/feature/<fb>
#   - push merge change:   git push … HEAD:refs/for/main   (the --no-ff merge commit)
#   - read votes:          GET  /a/changes/<n>/detail?o=DETAILED_LABELS
#   - submit requirements: GET  /a/changes/<n>?o=SUBMIT_REQUIREMENTS
#   - submit:              POST /a/changes/<n>/submit
#   - abandon (cleanup):   POST /a/changes/<n>/abandon
#   - create/delete fb:    PUT/DELETE /a/projects/rebar/branches/<url-encoded>
# The operator credential is read via `git credential fill` (osxkeychain / helper);
# it is NEVER echoed. This mirrors reviewbot-e2e.sh's conventions (XSSI-stripping
# api_get, the log/pass/harness/blocked terminal-state helpers, the vote/submit-
# requirement parsers, and the GitHub-replication poll) but generalizes the vote
# reader to BOTH labels and adds merge-change + branch-lifecycle + abandon steps.
#
# TERMINAL STATES (exit codes, caller-distinguishable — same convention as the
# sibling script):
#   - exit 0  PASS           — the scenario's assertions all held live.
#   - exit 2  HARNESS-FAILURE — the pipe could not be exercised (auth/push/REST/
#                              replication/timeout error). An infra problem, not a
#                              verdict.
#   - exit 3  GATE-BLOCKED   — a gate correctly declined (e.g. scenario 6: Verified=-1
#                              blocks submit on an empty auto-merge diff). For the
#                              scenarios whose EXPECTED outcome IS a block, this is
#                              the PASS condition and the scenario function returns 0
#                              after asserting the block; exit 3 is reserved for an
#                              UNEXPECTED block.
#
# Fail-closed throughout. Cleanup (trap) abandons every throwaway change this run
# created and deletes the e2e feature branch on both Gerrit and the GitHub mirror.
#
# DO NOT run in CI by default — it makes LIVE calls, pushes real (throwaway) changes,
# and submits merge changes to main on a THROWAWAY feature branch's content only
# (the merge changes are abandoned in cleanup unless a scenario explicitly submits
# one as part of its assertion, in which case it lands throwaway content that is
# immediately reverted — see per-scenario notes). Run by hand from an operator box.
#
# ---------------------------------------------------------------------------
# Env (all defaulted except none — the operator credential comes from the helper):
#   GERRIT_HOST           default rebar.solutions.navateam.com
#   GERRIT_USER           default JoeOakhartNava   (admin + feature-branch-drivers)
#   PROJECT               default rebar
#   REVIEW_BOT_NAME       default "rebar review bot"   (casts LLM-Review)
#   CI_BOT_NAME           default "rebar CI bot"        (casts Verified)
#   LLM_REVIEW_MAX        default 1
#   VERIFIED_MAX          default 1
#   GITHUB_REPO           default navapbc/rebar
#   FB                    default feature/e2e-<UTC-date>   (the throwaway branch)
#   POLL_TIMEOUT_SECONDS  default 480   (CI on Actions can take minutes)
#   POLL_INTERVAL_SECONDS default 8
#   SCENARIOS             default "1 2 3 4 5 6 7"   (space list; run a subset)
#   KEEP_FB               default ""    (set to 1 to skip feature-branch deletion)
# ---------------------------------------------------------------------------
set -euo pipefail

GERRIT_HOST="${GERRIT_HOST:-rebar.solutions.navateam.com}"
GERRIT_USER="${GERRIT_USER:-JoeOakhartNava}"
PROJECT="${PROJECT:-rebar}"
REVIEW_BOT_NAME="${REVIEW_BOT_NAME:-rebar review bot}"
CI_BOT_NAME="${CI_BOT_NAME:-rebar CI bot}"
LLM_REVIEW_MAX="${LLM_REVIEW_MAX:-1}"
VERIFIED_MAX="${VERIFIED_MAX:-1}"
GITHUB_REPO="${GITHUB_REPO:-navapbc/rebar}"
FB="${FB:-feature/e2e-$(date -u +%Y%m%d)}"
POLL_TIMEOUT_SECONDS="${POLL_TIMEOUT_SECONDS:-480}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-8}"
SCENARIOS="${SCENARIOS:-1 2 3 4 5 6 7}"
KEEP_FB="${KEEP_FB:-}"
# CI's "require resolvable rebar ticket" job fails a change (Verified=-1, real CI
# skipped) unless its commit message carries a resolvable rebar-ticket trailer — so
# every throwaway commit + merge commit this harness creates must reference S5's
# ticket. (This is itself the ticket-gate half of the two-vote gate; the scenarios
# that must PASS include the trailer, and any scenario deliberately proving a block
# uses a genuine code break, not a missing trailer.)
TICKET="${TICKET:-leafy-vogue-ingot}"
# shellcheck disable=SC2034  # TRAILER/TICKET are consumed by the sourced scenarios file
TRAILER=$'\n\nrebar-ticket: '"${TICKET}"

# --- logging + terminal-state helpers (same convention as reviewbot-e2e.sh) --
log()      { echo "fb-e2e: $*" >&2; }
pass()     { echo "fb-e2e: [PASS] $*" >&2; }
harness()  { echo "fb-e2e: [HARNESS-FAILURE] $*" >&2; exit 2; }
blocked()  { echo "fb-e2e: [GATE-BLOCKED] $*" >&2; exit 3; }

# --- operator credential (from the git credential helper; NEVER echoed) -------
_CRED="$(printf 'protocol=https\nhost=%s\nusername=%s\n\n' "$GERRIT_HOST" "$GERRIT_USER" \
	| git credential fill 2>/dev/null)" || harness "git credential fill failed for ${GERRIT_USER}@${GERRIT_HOST}"
GERRIT_PW="$(printf '%s' "$_CRED" | sed -n 's/^password=//p')"
[ -n "$GERRIT_PW" ] || harness "no password for ${GERRIT_USER} from the credential helper"
# Embed only the USERNAME in git URLs (a username-pinned credential-helper query is
# unambiguous) — the password may contain URL-reserved chars (/, +) that break a
# user:pw@host URL. git resolves the password via the helper; curl uses -u directly.
GIT_HTTPS_URL="https://${GERRIT_USER}@${GERRIT_HOST}/a/${PROJECT}"

# --- authenticated Gerrit REST helpers (strip the XSSI guard) ----------------
api_get()  { curl -sS -u "${GERRIT_USER}:${GERRIT_PW}" "https://${GERRIT_HOST}/a$1" | sed "1s/^)]}'//"; }
api_post() { curl -sS -X POST -u "${GERRIT_USER}:${GERRIT_PW}" \
	-H 'Content-Type: application/json' -d "${2:-{}}" "https://${GERRIT_HOST}/a$1" | sed "1s/^)]}'//"; }
api_put()  { curl -sS -X PUT -u "${GERRIT_USER}:${GERRIT_PW}" "https://${GERRIT_HOST}/a$1"; }
api_del()  { curl -sS -o /dev/null -w '%{http_code}' -X DELETE -u "${GERRIT_USER}:${GERRIT_PW}" "https://${GERRIT_HOST}/a$1"; }

# url-encode a ref for the branches REST endpoint (feature/x -> feature%2Fx)
urlenc()   { printf '%s' "$1" | sed 's#/#%2F#g'; }

# --- max integer vote a named account cast on a label ------------------------
# $1 = change number, $2 = label name, $3 = account display-name substring.
account_vote() {
	api_get "/changes/$1/detail?o=DETAILED_LABELS" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("NONE"); sys.exit(0)
label, who = sys.argv[1], sys.argv[2]
votes = (d.get("labels") or {}).get(label, {}).get("all") or []
vals = []
for v in votes:
    name = (v.get("name") or v.get("username") or "")
    if who.lower() in name.lower() and v.get("value") is not None:
        try: vals.append(int(v["value"]))
        except (TypeError, ValueError): pass
print(max(vals) if vals else "NONE")
' "$2" "$3"
}

# --- submit-requirement status for a named requirement -----------------------
sr_status() {  # $1 = change number, $2 = requirement name
	api_get "/changes/$1?o=SUBMIT_REQUIREMENTS" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("PARSE_ERROR"); sys.exit(0)
want = sys.argv[1]
for r in d.get("submit_requirements") or []:
    if r.get("name") == want:
        print(r.get("status") or "UNKNOWN"); break
else:
    print("ABSENT")
' "$2"
}

# --- change status / current patchset / current revision ---------------------
change_status()  { api_get "/changes/$1?o=CURRENT_REVISION" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("status") or "UNKNOWN")'; }
current_ps()     { api_get "/changes/$1?o=CURRENT_REVISION" | python3 -c 'import json,sys;d=json.load(sys.stdin);c=d.get("current_revision");print((d.get("revisions") or {}).get(c,{}).get("_number") or "")'; }
change_kind()    { api_get "/changes/$1?o=CURRENT_REVISION" | python3 -c 'import json,sys;d=json.load(sys.stdin);c=d.get("current_revision");print((d.get("revisions") or {}).get(c,{}).get("kind") or "UNKNOWN")'; }

# --- poll both votes until each reaches a terminal (non-NONE) value ----------
# Prints "LLM=<v> VER=<v>" once both are present or the deadline passes.
poll_both_votes() {  # $1 = change number
	local n="$1" deadline llm ver
	deadline=$(( $(date +%s) + POLL_TIMEOUT_SECONDS ))
	llm="NONE"; ver="NONE"
	while [ "$(date +%s)" -lt "$deadline" ]; do
		llm="$(account_vote "$n" LLM-Review "$REVIEW_BOT_NAME")"
		ver="$(account_vote "$n" Verified "$CI_BOT_NAME")"
		if [ "$llm" != "NONE" ] && [ "$ver" != "NONE" ]; then break; fi
		sleep "$POLL_INTERVAL_SECONDS"
	done
	echo "LLM=${llm} VER=${ver}"
}

# --- push HEAD as a change for review to a target ref ------------------------
# $1 = target (e.g. "refs/for/${FB}" or "refs/for/main"); prints change number.
push_for_review() {
	local target="$1" out num
	out="$(git push "$GIT_HTTPS_URL" "HEAD:${target}" 2>&1)" || { echo "$out" >&2; return 1; }
	echo "$out" >&2
	num="$(echo "$out" | grep -oE '/\+/[0-9]+' | head -n1 | grep -oE '[0-9]+')"
	[ -n "$num" ] || { echo "$out" >&2; return 1; }
	echo "$num"
}

submit_change() {  # $1 = change number
	local resp
	resp="$(api_post "/changes/$1/submit")"
	echo "$resp" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("status") or "UNKNOWN")' 2>/dev/null \
		|| { echo "$resp" >&2; return 1; }
}

# --- cleanup: abandon throwaway changes, delete the e2e feature branch --------
CREATED_CHANGES=()
EXTRA_BRANCHES=()   # dedicated per-scenario branches to delete on exit (e.g. S6's)
FB_CREATED=""
tmp="$(mktemp -d)"
cleanup() {
	set +e
	for c in "${CREATED_CHANGES[@]:-}"; do
		[ -z "$c" ] && continue
		local st; st="$(change_status "$c" 2>/dev/null || echo UNKNOWN)"
		if [ "$st" = "NEW" ] || [ "$st" = "DRAFT" ]; then
			api_post "/changes/$c/abandon" >/dev/null 2>&1
			log "cleanup: abandoned change #$c"
		fi
	done
	for b in "${EXTRA_BRANCHES[@]:-}"; do
		[ -z "$b" ] && continue
		api_del "/projects/${PROJECT}/branches/$(urlenc "$b")" >/dev/null 2>&1
		gh api -X DELETE "repos/${GITHUB_REPO}/git/refs/heads/${b}" >/dev/null 2>&1
		log "cleanup: deleted dedicated branch ${b}"
	done
	if [ -n "$FB_CREATED" ] && [ -z "$KEEP_FB" ]; then
		local code; code="$(api_del "/projects/${PROJECT}/branches/$(urlenc "$FB")" 2>/dev/null)"
		log "cleanup: deleted Gerrit branch ${FB} (http ${code})"
		gh api -X DELETE "repos/${GITHUB_REPO}/git/refs/heads/${FB}" >/dev/null 2>&1 \
			&& log "cleanup: deleted GitHub mirror branch ${FB}" || true
	fi
	rm -rf "$tmp"
}
trap cleanup EXIT

# --- ensure the throwaway feature branch exists (create from main) -----------
ensure_fb() {
	local exists
	exists="$(git ls-remote "$GIT_HTTPS_URL" "refs/heads/${FB}" | awk '{print $1}')"
	if [ -n "$exists" ]; then
		log "feature branch ${FB} already exists (reusing) at ${exists}"
		return
	fi
	# create from current main via the branches REST endpoint
	local main_sha
	main_sha="$(git ls-remote "$GIT_HTTPS_URL" refs/heads/main | awk '{print $1}')"
	api_put "/projects/${PROJECT}/branches/$(urlenc "$FB")" >/dev/null
	FB_CREATED=1
	log "created feature branch ${FB} from main (${main_sha})"
	# WAIT for the branch to replicate to the GitHub mirror BEFORE any change is
	# pushed — g2p's CI dispatch is workflow_dispatch(ref=refs/heads/${FB}), which
	# 404s (no run, no Verified) if GitHub does not yet have the ref. This was the
	# race that left the first feature-branch change with LLM-Review but no CI.
	local deadline; deadline=$(( $(date +%s) + 120 ))
	while [ "$(date +%s)" -lt "$deadline" ]; do
		[ -n "$(git ls-remote "https://github.com/${GITHUB_REPO}" "refs/heads/${FB}" | awk '{print $1}')" ] && { log "feature branch ${FB} replicated to GitHub"; return; }
		sleep 4
	done
	harness "feature branch ${FB} did not replicate to GitHub within 120s (CI dispatch would race)"
}

# --- a throwaway repo clone for building commits ------------------------------
clone_repo() {
	git clone -q "$GIT_HTTPS_URL" "$tmp/repo" || harness "clone failed"
	cd "$tmp/repo"
	curl -sS -Lo .git/hooks/commit-msg -u "${GERRIT_USER}:${GERRIT_PW}" \
		"https://${GERRIT_HOST}/tools/hooks/commit-msg" \
		|| harness "could not fetch commit-msg hook"
	chmod +x .git/hooks/commit-msg
	git config user.email "fb-e2e@navateam.com"
	git config user.name  "feature-branch e2e"

	# DCO: after the requireSignedOffBy flip Gerrit rejects unsigned pushes to
	# refs/for/*. Every commit the sourced scenarios make is signed at its call site
	# (`git commit -s`) and every merge with `git merge --signoff`; this
	# prepare-commit-msg hook is the belt-and-braces net that also signs commits a
	# future scenario might add without -s (and merge commits, which cannot take -s).
	# It is idempotent: --if-exists doNothing skips a trailer that -s already added.
	cat > .git/hooks/prepare-commit-msg <<-'HOOK'
		#!/bin/sh
		git interpret-trailers --if-exists doNothing \
			--trailer "Signed-off-by: feature-branch e2e <fb-e2e@navateam.com>" \
			--in-place "$1"
	HOOK
	chmod +x .git/hooks/prepare-commit-msg
}

# =============================================================================
# The scenario functions live in feature-branch-e2e-scenarios.sh (sourced) so this
# driver stays under the module-size cap and the scenarios read as a unit.
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "${HERE}/feature-branch-e2e-scenarios.sh"

log "=== feature-branch E2E — host=${GERRIT_HOST} fb=${FB} scenarios='${SCENARIOS}' ==="
clone_repo
ensure_fb

rc=0
for s in $SCENARIOS; do
	log "----- scenario ${s} -----"
	if "scenario_${s}"; then
		pass "scenario ${s} PASSED"
	else
		log "[FAIL] scenario ${s} did not pass"
		rc=1
	fi
done

if [ "$rc" -eq 0 ]; then
	echo "fb-e2e: all requested scenarios PASSED" >&2
else
	echo "fb-e2e: one or more scenarios FAILED" >&2
fi
exit "$rc"
