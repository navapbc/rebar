# `.github/complexity-baseline.json` — shrink-only, locked against `main`

`complexity-baseline.json` is a per-symbol C901 (McCabe cyclomatic-complexity) ceiling
table. The baseline is **shrink-only** and **locked against `main`** by the
"Complexity-baseline lock" CI step in `_build-and-test.yml`.

## What is allowed and what is not

| Change to `ceilings` | Verdict |
|---|---|
| Remove an entry | ✅ Allowed — the ratchet working |
| Lower an existing ceiling | ✅ Allowed — the ratchet working |
| Raise an existing ceiling | ❌ Rejected — administrator override required |
| Add a new entry | ❌ Rejected — administrator override required |

## How to intentionally raise or add (administrator only)

An administrator must **force-submit** the Gerrit change to bypass the `Verified` gate.
This is intentional: raising a ceiling or adding a new high-complexity function is a
deliberate architectural decision that needs explicit human sign-off.

## Fail-closed policy

The lock **fails closed**: if the CI runner cannot fetch the `main` copy of the baseline
(network error, repo misconfiguration, etc.) the gate exits nonzero. There is no
warn-and-continue fallback.

## Updating the baseline (draining stale debt)

To remove stale entries or lower ceilings after refactoring, run:

```
python scripts/check_complexity_baseline.py --update-stale
```

This lowers ceilings that dropped below their recorded value and removes entries whose
function no longer exceeds the threshold. It refuses to run when new or increased
complexity debt is present.
