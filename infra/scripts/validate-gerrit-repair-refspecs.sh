#!/usr/bin/env bash
# Reject Gerrit object-store repair refspecs that write mirror state into live branches.
set -euo pipefail

if [ "$#" -eq 0 ]; then
	echo "usage: validate-gerrit-repair-refspecs.sh <fetch-refspec>..." >&2
	exit 2
fi

bad=0
for raw in "$@"; do
	refspec="${raw#+}"
	case "$refspec" in
		*:refs/heads | *:refs/heads/*)
			echo "refused repair refspec '$raw': destination refs/heads/* is protected; fetch into refs/recovery/github/*" >&2
			bad=1
			;;
	esac
done

exit "$bad"
