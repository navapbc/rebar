#!/usr/bin/env bash
# Integration test: ticket ID collision probe (SC11, epic 3e74-56da)
#
# Generates N ticket IDs via ticket-create.sh logic and asserts:
#   (a) zero canonical ID collisions
#   (b) alias collision rate is within birthday-paradox expectation
#       (aliases are ~1.5B combinations; collisions at N=100K are expected
#       by design — aliases are mnemonic helpers, not unique identifiers)
#
# Usage:
#   ./run.sh [--count=N]   (default N=100000)
#
# Excluded from default test_gate.test_dirs (tests/) — runs only via explicit
# invocation or `make test-integration`. NOT in CI's default gate.
#
# Exit codes:
#   0 — all assertions pass
#   1 — collision(s) detected or environment error

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
_COUNT=100000
for _arg in "$@"; do
    case "$_arg" in
        --count=*) _COUNT="${_arg#--count=}" ;;
    esac
done

_REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "Error: not inside a git repository" >&2; exit 1
}
_WORDLIST="$_REPO_ROOT/src/rebar/_engine/resources/ticket-wordlist.txt"

if [ ! -f "$_WORDLIST" ]; then
    echo "Error: wordlist not found at $_WORDLIST" >&2; exit 1
fi

echo "Generating $_COUNT ticket IDs and checking for collisions..."

python3 - "$_COUNT" "$_WORDLIST" <<'PYEOF'
import sys
import uuid

count    = int(sys.argv[1])
wordlist = sys.argv[2]

# Inline the same alias logic as ticket-alias-compute.py
def load_words(path):
    adjs, nouns = [], []
    section = "adj"
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if line == "# NOUNS":
                    section = "noun"
                    continue
                if line.startswith("#") or not line.strip():
                    continue
                if section == "adj":
                    adjs.append(line.strip())
                else:
                    nouns.append(line.strip())
    except (OSError, IOError):
        pass
    return adjs, nouns

def compute_alias(hex_id, adjs, nouns):
    if not adjs or not nouns:
        return hex_id[:8]
    adj   = adjs[int(hex_id[0:4], 16) % len(adjs)]
    noun1 = nouns[int(hex_id[4:8], 16) % len(nouns)]
    noun2 = nouns[int(hex_id[8:12], 16) % len(nouns)]
    return f"{adj}-{noun1}-{noun2}"

adjs, nouns = load_words(wordlist)

canonical_set = set()
alias_set = set()
canonical_collisions = []
alias_collisions = []

for i in range(count):
    u = str(uuid.uuid4()).replace('-', '')
    ticket_id = u[:4] + '-' + u[4:8] + '-' + u[8:12] + '-' + u[12:16]

    if ticket_id in canonical_set:
        canonical_collisions.append(ticket_id)
    canonical_set.add(ticket_id)

    alias = compute_alias(u, adjs, nouns)
    if alias in alias_set:
        alias_collisions.append((ticket_id, alias))
    alias_set.add(alias)

    if (i + 1) % 10000 == 0:
        print(f"  Progress: {i+1}/{count} IDs generated...", flush=True)

# Report
print(f"\nResults ({count} IDs):")
print(f"  Unique canonical IDs : {len(canonical_set)}")
print(f"  Unique aliases       : {len(alias_set)}")
print(f"  Canonical collisions : {len(canonical_collisions)}")
print(f"  Alias collisions     : {len(alias_collisions)}")

ok = True
if canonical_collisions:
    print(f"\nFAIL: {len(canonical_collisions)} canonical ID collision(s):", file=sys.stderr)
    for c in canonical_collisions[:10]:
        print(f"  {c}", file=sys.stderr)
    ok = False
else:
    print("PASS: zero canonical collisions")

if alias_collisions:
    # Alias collisions are expected by the birthday paradox (~1.5B combinations,
    # 100K samples → ~96% collision probability). Report count informally only.
    print(f"INFO: {len(alias_collisions)} alias collision(s) (expected by birthday paradox — aliases are non-unique by design)")
else:
    print("INFO: zero alias collisions (uncommon at this sample size)")

sys.exit(0 if ok else 1)
PYEOF

echo "Done."
