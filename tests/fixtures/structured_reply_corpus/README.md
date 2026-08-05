# structured_reply_corpus

Captured completion-verifier reply shapes for the schema-blind-extraction fix
(bug df3a / epic candied-snippy-schnauzer). Each `*.json` describes one reply the
completion verifier produced (or a faithful layout variant of the captured b586 reply),
where a **dependency-link record quoted from a `show_ticket` tool result** appears in the
transcript alongside the real `CompletionVerdict` payload.

Schema:

```json
{
  "id": "...",                 // stable variant id
  "description": "...",        // what layout this exercises
  "fails_on_first_wins": true, // true iff today's first-parseable-object parser mis-selects
  "expected_verdict": "PASS",  // the verdict the REAL payload carries (post-normalization)
  "reply": "...raw model reply text..."
}
```

The decoy dep-link records carry keys (`relation`, `target_id`, `link_uuid`) that share
**zero** top-level keys with `CompletionVerdict` (`verdict`, `findings`, `criteria`,
`summary`), so the schema-filtered selection screens them out; the real payload is rendered
**last** in every variant, so last-valid-wins recovers it.
