#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# feature-branch-inventory.sh — the stale-`feature/*`-branch inventory for the
# epic-88ab feature-branch flow (story norm-dam-swab / S6). The pattern's known
# failure mode is branch rot: OpenDev warns feature branches are "not for sustained
# long-term development" and Qt abandoned routine long-lived-branch merges. Gerrit
# does not auto-prune a merged-back or abandoned `feature/*` branch, so this scripted
# check flags branches inactive beyond the lifetime cap — whether MERGED-BACK (already
# folded into main) or ABANDONED (never landed) — for owner-confirmed deletion, and is
# the tooling half of the S6 lifetime/cadence policy (the prose lives in CONTRIBUTING §4
# and infra/runbooks/review-bot-ops.md).
#
# READ-ONLY by default (prints the inventory). Deletion is never automatic — it prints
# the exact `curl -X DELETE` per stale branch for an OWNER to confirm and run, because a
# feature branch may be paused rather than dead (the policy is owner-confirmed, not a cron
# that reaps). Pass --delete to delete AFTER you have confirmed ownership (still one branch
# at a time, each logged).
#
# Drives over HTTPS/REST as an operator (feature-branch-drivers / admin), same auth model
# as feature-branch-e2e.sh: the credential comes from `git credential fill`, never echoed.
#
# Env (all defaulted):
#   GERRIT_HOST   default rebar.solutions.navateam.com
#   GERRIT_USER   default JoeOakhartNava
#   PROJECT       default rebar
#   CAP_DAYS      default 14   (the branch-lifetime cap; older + inactive => flagged)
#   DELETE        default ""   (set / pass --delete to actually delete flagged branches)
# ---------------------------------------------------------------------------
set -euo pipefail

GERRIT_HOST="${GERRIT_HOST:-rebar.solutions.navateam.com}"
GERRIT_USER="${GERRIT_USER:-JoeOakhartNava}"
PROJECT="${PROJECT:-rebar}"
CAP_DAYS="${CAP_DAYS:-14}"
DELETE="${DELETE:-}"
[ "${1:-}" = "--delete" ] && DELETE=1
NOW_EPOCH="${NOW_EPOCH:-$(date -u +%s)}"   # overridable so the inventory is testable

_cred="$(printf 'protocol=https\nhost=%s\nusername=%s\n\n' "$GERRIT_HOST" "$GERRIT_USER" \
	| git credential fill 2>/dev/null)" || { echo "credential fill failed" >&2; exit 2; }
PW="$(printf '%s' "$_cred" | sed -n 's/^password=//p')"
[ -n "$PW" ] || { echo "no password for ${GERRIT_USER}" >&2; exit 2; }

api() { curl -sS -u "${GERRIT_USER}:${PW}" "https://${GERRIT_HOST}/a$1" | sed "1s/^)]}'//"; }
urlenc() { printf '%s' "$1" | sed 's#/#%2F#g'; }

main_sha="$(git ls-remote "https://${GERRIT_USER}@${GERRIT_HOST}/a/${PROJECT}" refs/heads/main | awk '{print $1}')"

echo "feature-branch inventory — project=${PROJECT} cap=${CAP_DAYS}d main=${main_sha:0:12}"
echo "----------------------------------------------------------------------------"

# List every feature/* branch (Gerrit REST returns refs/heads/feature/... entries).
branches="$(api "/projects/${PROJECT}/branches/" | python3 -c '
import json, sys
for b in json.load(sys.stdin):
    ref = b.get("ref", "")
    if ref.startswith("refs/heads/feature/"):
        print(ref[len("refs/heads/"):], b.get("revision", ""))
')"

[ -n "$branches" ] || { echo "no feature/* branches — nothing to inventory"; exit 0; }

stale=()
while read -r fb rev; do
	[ -z "$fb" ] && continue
	# last-commit author date (epoch) for the branch tip, via the commit REST endpoint.
	committed="$(api "/projects/${PROJECT}/commits/${rev}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
# committer.date is "YYYY-MM-DD HH:MM:SS.000000000" (UTC)
ds = (d.get("committer") or {}).get("date") or ""
import calendar, time
try:
    t = time.strptime(ds.split(".")[0], "%Y-%m-%d %H:%M:%S")
    print(int(calendar.timegm(t)))
except Exception:
    print(0)
')"
	age_days=$(( (NOW_EPOCH - committed) / 86400 ))
	# merged-back? tip is an ancestor of main  =>  already folded in, safe to prune.
	if git merge-base --is-ancestor "$rev" "$main_sha" 2>/dev/null; then
		state="MERGED-BACK"
	else
		state="ABANDONED?"   # not in main's history — either in-flight or truly abandoned
	fi
	flag=""
	if [ "$age_days" -ge "$CAP_DAYS" ]; then flag="  <== STALE (>= ${CAP_DAYS}d)"; stale+=("$fb"); fi
	printf '%-40s %-12s age=%3dd %s%s\n' "$fb" "$state" "$age_days" "${rev:0:12}" "$flag"
done <<< "$branches"

echo "----------------------------------------------------------------------------"
if [ "${#stale[@]}" -eq 0 ]; then
	echo "no branches past the ${CAP_DAYS}d cap"
	exit 0
fi
echo "${#stale[@]} branch(es) past the cap. Owner-confirmed deletion (Gerrit + GitHub mirror):"
for fb in "${stale[@]}"; do
	if [ -n "$DELETE" ]; then
		code="$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -u "${GERRIT_USER}:${PW}" \
			"https://${GERRIT_HOST}/a/projects/${PROJECT}/branches/$(urlenc "$fb")")"
		echo "  deleted ${fb} (gerrit http ${code})"
		gh api -X DELETE "repos/navapbc/${PROJECT}/git/refs/heads/${fb}" >/dev/null 2>&1 \
			&& echo "    + deleted GitHub mirror ${fb}" || true
	else
		echo "  # ${fb}:"
		echo "  curl -X DELETE -u '${GERRIT_USER}:<pw>' 'https://${GERRIT_HOST}/a/projects/${PROJECT}/branches/$(urlenc "$fb")'"
		echo "  gh api -X DELETE 'repos/navapbc/${PROJECT}/git/refs/heads/${fb}'   # mirror (mirror=false => manual)"
	fi
done
[ -z "$DELETE" ] && echo "(re-run with --delete to execute, after confirming each branch's owner)"
