# Session log — OSS ticket-system comparison → remediation plan → epics

**Date:** 2026-06-13 · **Branch:** `claude/oss-ticket-systems-analysis-g857w0`

## Objective

Compare rebar to popular OSS ticket systems, identify gaps / gotchas / unfollowed
best practices, propose a value-vs-risk remediation strategy, then harden that into
a review- and experiment-validated implementation plan and file it as rebar epics.

## Deliverables produced

| Artifact | Path |
|----------|------|
| OSS comparison + remediation strategy | `docs/oss-comparison-and-remediation.md` |
| Detailed implementation plan (validated) | `docs/remediation-implementation-plan.md` |
| Reproducible experiments + stdlib guard artifact | `docs/experiments/` (`README.md`, `hlc_prototype.py`, `event_write_guard.py`) |
| 7 rebar epics (shared `tickets` store) | tagged `oss-remediation` (ids below) |
| This session log | `docs/2026-06-13-oss-comparison-session-log.md` |

## 1. Comparison & analysis

Compared rebar against git-bug, git-issue (dspinellis), Fossil tickets,
dstask/taskwarrior, with Redmine/GitLab/Fossil as best-practice references.

- **Where rebar leads (preserve):** atomic `claim`, conflict-aware `next-batch`
  file-impact scheduling, per-ticket quality gates, three-interface parity with
  JSON-Schema + golden tests, deterministic UUID-keyed STATUS-fork resolution,
  preserve-and-ignore forward compatibility, MCP-native.
- **Gotchas others handle that rebar didn't:** G1 wall-clock (not logical) ordering
  of EDIT/COMMENT under skew (I8); G2 unauthenticated identity; G3 unbounded git
  object growth with `gc.auto=0` and no reclaim path; G4 whole-field collection
  clobbering; G5 substring-only search.
- **Functional gaps:** no human read UI, GitHub/GitLab bridge, export/import,
  due dates/milestones, watchers/notifications, attachments.

## 2. Plan + adversarial review loop (7 rounds, Opus reviewer)

Wrote `docs/remediation-implementation-plan.md` and ran it through an
adversarial review-and-remediate loop until **no MAJOR findings**:

- R1: 6 MAJOR (false tag-delta premise; HLC width/sort/topology/lock bugs;
  signing-vs-compaction incoherence; missing byte-parity gate) → fixed.
- R2: HLC core confirmed sound; 2 MAJOR + minors (four serializers not two;
  incomplete sort-site list; dual-write clobber; SNAPSHOT identity) → fixed.
- R3: pre-migration tag-loss seeding; fifth+ serializers → pivot to structural guard.
- R4–R6: enumeration is a losing game (15+ serializers; many dead leaf scripts);
  found live writers `ticket-comment.sh` (force-close) and `ticket-revert.sh`
  (un-archive) reached via `ticket-transition.sh`; made the guard authoritative.
- R7: **MAJOR findings: no** — live-writer set machine-verifiable and complete.

## 3. Experimental validation + proven-art convergence

Prototyped every surviving change and cross-checked proven OSS designs
(git-bug clock+identity, Riak/Akka OR-Set, Automerge/Yjs seeding, TUF/sigstore
canonicalization, git-bug/Radicle/Fossil label-merge). Results in the plan's
"Experimental validation" section; scripts in `docs/experiments/`.

**Two designs changed from proven art:**
- **P2.3 dropped the OR-Set** for git-bug's `LabelChangeOperation` delta-replay-order
  model — simpler, proven, and avoids three hazards (unbounded tombstones;
  causal-stability-gated compaction, which rebar can't detect; Yjs/Automerge
  independent-seed duplication).
- **P1.0/P2.1 collapse to one Python serializer** — `jq` parses the >2^53 ns
  timestamp as float64 (rounds it; ≤1.6 on parse, 1.7 on arithmetic), so the
  `json.dumps==jq -S -c` parity claim was unsafe and could corrupt the ordering key.

**Real-code de-risking (installed rebar + live `.tickets-tracker`):**
- EXP-R1: **reproduced the tag-clobber bug through the real reducer** (one add lost);
  EXP-R5: delta fix resolves it.
- EXP-R2: `event_sort_key` int change safe on real filenames.
- EXP-R3: real `_canonical_bytes` ≠ plain dumps; re-serialize replay-safe.
- EXP-R4: real reducer preserve-ignores an unknown `TAG` event.
- EXP-R6: gc recipe packs the real orphan worktree 26→0; reads survive.
- EXP-R7: `python3 -m rebar._store.<sub>` works (bash→helper seam).
- EXP-R9/R9b: stdlib guard flags exactly 7 py + 7 sh writers, 0 false positives
  (semgrep/ast-grep/pre-commit are absent → guard must be stdlib).
- EXP-R10/R11: confirmed query signatures; 31 reducer tests pass in 2.5 s.
- EXP-R8: host force-signs commits; rebar writes still succeed → latent note.

## 4. Surviving changes → 7 epics (rebar store, tag `oss-remediation`)

| Epic | Title | Priority | ID | Blocked by |
|------|-------|----------|----|-----------|
| P1.0 | Unify canonical event-byte serialization + stdlib structural guard | 1 (prereq) | `0b32-bc94-5ea1-482a` | — |
| P1.1 | Structured query: predicates, OR, negation, `--sort` | 2 | `31e8-c843-cc28-46d4` | — |
| P1.2 | `rebar export` / `import` (JSON + GitHub issues) | 2 | `ab74-b205-72af-4ce1` | — |
| P1.4 | `rebar gc` + maintenance doctrine | 2 | `a240-d692-a86f-411c` | — |
| P2.1 | Hybrid Logical Clock for event ordering | 1 | `7d1d-6b06-de2a-4836` | P1.0 |
| P2.2 | Authenticated identity + optional detached signature | 2 | `e68d-b701-4dd9-47aa` | P1.0 |
| P2.3 | Tag convergence via delta events + deterministic replay order | 2 | `0d8f-f43e-c741-4cce` | P1.0, P2.1 |

Each epic carries `## Context` (problem + research), `## Evidence` (experiments,
incl. real-code EXP-R*), `## What / Implementation detail` (seams with file:line),
`## Success Criteria`, and a `## Acceptance Criteria` checklist; all pass `check_ac`
and `clarity_check`. `set_file_impact` recorded per epic for `next-batch`. Blocking
links encode the order (P1.0 → P2.1/P2.2/P2.3; P2.1 → P2.3); P1.0/P1.1/P1.2/P1.4 are
`ready`. The set `relates_to` the open architecture epic `6001-b906-aac6-4fa4`.

**Recommended cut line:** P1.0 first (prerequisite; guard already written), then
P1.1/P1.2/P1.4 in parallel, then P2.1 (gated on its skewed-clock convergence test
merged red-first), then P2.3 + P2.2.

## Environment notes

- `pip install -e .` works; base install lacks `pytest` (needs `[dev]`).
- Host forces SSH commit signing (`commit.gpgsign=true`); rebar writes still
  succeed via the env's signing server (stderr noise) — see P2.2.
- `jq` 1.7, `flock`, `git` 2.43 present; `semgrep`/`ast-grep`/`pre-commit` absent.
