# The `land` agent contract (auto-lander, epic f1fa)

`land` / `land-status` collapse an agent's landing interaction to **one call → one typed
terminal outcome + a distinct exit code**. Agents depend ONLY on this contract — never on
correlating Gerrit votes/labels/status themselves. The overall auto-lander design is recorded
in ADR-0042 (authored by S6); this file is the stable, versioned *interface* contract.

**`contract-version: 1`**

## Outcomes and exit codes

Every exit code is pinned — there is no "any nonzero" hole.

| Outcome         | Exit | Meaning | Which command |
|-----------------|------|---------|---------------|
| `merged`        | 0    | The change/stack landed on `main`. | both |
| `needs_rebase`  | 1    | Behind tip / rebase conflict — rebase to tip and re-request. (Only from the bot's recorded outcome; NOT derivable from native state.) | both |
| `ci_failed`     | 2    | Post-rebase `Verified -1` (survived the auto-recheck). | both |
| `review_failed` | 3    | `LLM-Review -1`. | both |
| `not_requested` | 4    | No `Autosubmit` set and no bot record — run `land` first. | `land-status` only |
| `abandoned`     | 5    | The Gerrit change is `ABANDONED`. | both |
| `lander_down`   | 6    | The single-instance bot's heartbeat is stale (>90 s) or its status endpoint is unreachable — land manually (FFO rebase + submit). | both |
| `error`         | 7    | Any other failure (bounded transport retries exhausted, etc.). | both |
| `pending`       | 75   | The bot is still driving (non-terminal). | `land-status` only |
| `timed_out`     | 124  | `--wait` exceeded the overall timeout (default 30 min). | `land --wait` |

## JSON output schema

`land` / `land-status` print one JSON object to stdout (the machine interface):

```json
{
  "outcome": "<one of the outcomes above>",
  "change": "<the change id/number passed in>",
  "detail": "<optional human-readable detail, e.g. the lander_down guidance>",
  "contract_version": "1"
}
```

`outcome`, `change`, and `contract_version` are always present; `detail` is optional.

## Behaviour

- **`land <change> --wait`** checks the bot heartbeat FIRST; if already stale/unreachable it
  returns `lander_down` **without** setting `Autosubmit` (never orphan a label the down bot
  won't consume). When fresh, it sets `Autosubmit` under the **agent's own** Gerrit identity
  (requires S1's requester-votable ACL), blocks until terminal, and returns the outcome.
- **`land-status <change>`** is a single-shot read (never sets `Autosubmit`); it adds
  `pending` and `not_requested`.
- **Fallback precedence:** the bot's recorded outcome (S3) wins if present; else derive from
  native Gerrit state (`MERGED`→merged, `ABANDONED`→abandoned, `Verified -1`→ci_failed,
  `LLM-Review -1`→review_failed; if both `-1` with no record, ci_failed wins). `needs_rebase`
  is only ever the bot's recorded outcome.
- **Liveness:** heartbeat staleness threshold 90 s (6× the bot's 15 s poll); an unreachable
  status endpoint is treated conservatively as `lander_down`, not `error`. These thresholds
  (90 s stale, 30 s poll, 30 min timeout) are config values, not hard-coded literals.

## Stability

This contract is versioned. Additive changes (new optional JSON fields) keep the version;
any change to the outcome set, exit codes, or required fields bumps `contract-version`.
`land --help` cites this file.
