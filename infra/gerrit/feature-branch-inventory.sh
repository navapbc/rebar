#!/usr/bin/env bash
# Inventory feature branches older than CAP_DAYS (default 14) and inactive.
# Classify each as ancestor-of-main, content-equivalent after rebase, or absent
# from main. The state is advisory. Owners confirm each deletion. Read-only by
# default. --delete/DELETE performs confirmed Gerrit and GitHub removal.
# HTTPS auth comes from git credential fill and is never printed. GERRIT_HOST,
# GERRIT_USER, PROJECT, CAP_DAYS, and DELETE may be overridden.
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

content_matches_main() {
	rev="$1"
	paths="$(git diff --name-only "${main_sha}...${rev}" 2>/dev/null)" || return 1
	[ -z "$paths" ] && return 0
	oldIFS="$IFS"; IFS='
'
	set -f
	# shellcheck disable=SC2086 # intentional newline-only splitting after setting IFS above
	set -- $paths
	set +f
	IFS="$oldIFS"
	git diff --quiet "$main_sha" "$rev" -- "$@" 2>/dev/null
}

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
	elif content_matches_main "$rev"; then
		state="MERGED-REBASED"
	else
		state="NOT-IN-MAIN"
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
		echo "  # ${fb}: confirm the owner, then delete both sides —"
		echo "  #   Gerrit:  DELETE /a/projects/${PROJECT}/branches/$(urlenc "$fb")  (auth as a feature-branch-driver)"
		echo "  #   GitHub:  gh api -X DELETE repos/navapbc/${PROJECT}/git/refs/heads/${fb}   (mirror=false => manual)"
		echo "  #   or just re-run this script with --delete once every listed branch is owner-confirmed."
	fi
done
[ -z "$DELETE" ] && echo "(re-run with --delete to execute, after confirming each branch's owner)"
