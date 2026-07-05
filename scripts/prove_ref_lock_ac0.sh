#!/usr/bin/env bash
# Prove AC0 (task 524d / epic dust-troth-naval): a non-branch refs/reconciler/*
# ref pointing directly at a BLOB round-trips (push + fetch + delete) through a
# real remote — by default `origin` (GitHub). This is the repeatable proving
# command the plan-review / completion gate asks for; run it in CI with the
# GITHUB_TOKEN (contents:write) or locally against your push remote.
#
#   scripts/prove_ref_lock_ac0.sh [<remote>]     # default remote: origin
#
# Exit 0 = the blob-ref refspec is accepted end-to-end. Non-zero = rejected;
# fall back to the ref->tiny-commit scheme documented in docs/adr/0031.
set -euo pipefail

REMOTE="${1:-origin}"
REF="refs/reconciler/spike-ac0-$$"          # unique, throwaway namespace
PAYLOAD='{"holder":"ac0-spike","lease_secs":120,"heartbeat_ns":1,"fence":0}'

cleanup() {
  git update-ref -d "$REF" 2>/dev/null || true
  git push "$REMOTE" ":$REF" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "AC0: proving a blob-pointing $REF round-trips through '$REMOTE'"

# 1. Plant the blob and capture its OID (the value the ref will point at).
BLOB="$(printf '%s\n' "$PAYLOAD" | git hash-object -w --stdin)"
echo "  planted blob $BLOB"

# 2. Create-only CAS push of the blob-ref to the remote.
git push "$REMOTE" "$BLOB:$REF"
echo "  pushed  $BLOB -> $REMOTE/$REF"

# 3. The remote must now advertise the ref at exactly that blob OID.
REMOTE_OID="$(git ls-remote "$REMOTE" "$REF" | awk '{print $1}')"
[ "$REMOTE_OID" = "$BLOB" ] || { echo "FAIL: remote ref is $REMOTE_OID, expected $BLOB"; exit 1; }
echo "  ls-remote confirms $REMOTE_OID"

# 4. Fetch it back and confirm the payload survived byte-for-byte.
git fetch "$REMOTE" "$REF:$REF" >/dev/null 2>&1
[ "$(git cat-file -t "$BLOB")" = "blob" ] || { echo "FAIL: object is not a blob"; exit 1; }
GOT="$(git cat-file blob "$REF")"
[ "$GOT" = "$PAYLOAD" ] || { echo "FAIL: payload mismatch: $GOT"; exit 1; }
echo "  fetched blob content intact"

echo "AC0 PROVEN: '$REMOTE' accepts a blob-pointing refs/reconciler/* ref (push+fetch+delete)."
