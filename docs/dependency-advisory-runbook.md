# Dependency-advisory runbook

**You are probably here because a `pip-audit` / dependency-advisory step went red.** Start
with §1 — most of the time the answer is "this is not yours to fix".

rebar commits `uv.lock`, which is the right call for reproducibility but has one
consequence: every transitive dependency is frozen, so the moment an advisory is
published against a pinned version, a naively-gating scan reddens every change in flight
at once, authored by people who neither caused the advisory nor can fix it. That is
exactly what happened with `click 8.2.1` / PYSEC-2026-2132 — six changes across five work
streams went red simultaneously and each author independently started diagnosing it
(bug `63e8-9235-220f-4201`).

The gate was not weakened in response: it found a real advisory in the shipped
environment. What changed is **who owns the finding**.

---

## 1. Is this mine? — the lane rules

| Lane | Blocks on a CRITICAL/HIGH advisory? |
|---|---|
| **Gerrit `Verified`** — your change under review | **Only if your change touches the dependency map** |
| **Branch / scheduled `main` CI** (the GitHub mirror) | Always — but that red blocks no merge and no Gerrit submit |
| **Release** (`release.yml`) | Always. A release never ships on a known-vulnerable pin |

**"Touches the dependency map"** means the change edits `uv.lock`, a `pyproject.toml`
(anywhere in the tree), a `requirements*.txt`, or a `constraints*.txt`. The rule is *if
you touch it, you own it*: an author already editing the dependency map is in position to
resolve the finding, and is the right owner for whatever the resulting closure contains —
including an advisory their edit did not introduce. This is deliberately the dependency
**map**, not a per-package comparison against the advisory's own package: a finer match
would let a dependency-map change land while the closure it produced was still vulnerable.

So:

* **Your Gerrit change does not touch `uv.lock` / `pyproject`?** The advisory is printed
  as a warning and your job stays green. Nothing to do. It is owned by the scheduled
  dependency-advisory canary, which files a ticket for it.
* **Your Gerrit change does touch the dependency map?** It is yours. Go to §3.
* **You are cutting a release and it is blocked?** It is yours. Go to §3.

If the changed-file diff cannot be resolved (a checkout too shallow to see the patchset's
parent), the gate **fails closed** and treats the change as dependency-map-touching. An
unknown diff must never be the reason an advisory goes unblocked.

## 2. Real finding, or advisory-DB flake?

**Only a DB-unreachable failure is recheckable. `recheck` can NEVER clear a real
finding** — this is the single most time-wasting property recorded on the original bug,
because `recheck` is everyone's first instinct.

* **Infrastructure**: the step says *"advisory DB unreachable after N attempts —
  INFRASTRUCTURE issue, not a vulnerability"*. The fetch is already retried 3× with
  backoff, so seeing this means all three failed. Re-run the job / comment `recheck`.
* **Real finding**: the step lists advisory ids, packages, and versions. Re-running
  produces exactly the same output. Go to §3.

## 3. Severity bar

The common OSS bar, chosen so developers are not blocked on low-risk findings:

| Severity | Effect |
|---|---|
| CRITICAL, HIGH | **fail** the lane (subject to §1) |
| MEDIUM / MODERATE | warn — annotation only, never blocking |
| LOW | tracked — printed, never blocking |
| **no severity attached** | **treated as HIGH → fail** |

That last row is the deliberate fallback, and it matters: **pip-audit carries no severity
at all** — its `VulnerabilityResult` is `(id, description, fix_versions, aliases,
published)`. Severity is therefore enriched from OSV (`database_specific.severity`) at
scan time, and enrichment is **fail-soft**: any error, timeout, or missing field yields no
severity, which lands on the HIGH fallback. An OSV outage makes the gate **stricter**,
never weaker, and an unrated advisory is never silently ignored.

## 4. Remediation, in order

### (a) Upgrade the direct dependency

Always try this first. If rebar declares the package itself, raise the floor in
`pyproject.toml` and re-lock:

```sh
uv lock --upgrade-package <pkg>
uv sync --locked --extra dev
```

If that clears the finding, you are done — go to §5 (verification).

### (b) An upstream cap is blocking the upgrade

This is the click/PYSEC-2026-2132 shape: rebar does not depend on the vulnerable package
directly; some *other* dependency caps it (there, `inspect-ai` capped `click`), and uv's
universal resolution locks every extra jointly, so one capping dependency holds the whole
project on the vulnerable version.

**Three things that look like progress and are not** — each of these cost real time on
the original incident, so do not rediscover them under pressure:

1. **`uv lock --upgrade-package <pkg>` is a NO-OP against an upstream cap.** It asks the
   resolver to prefer a newer version; the cap still forbids it, so the lock does not move
   and nothing tells you why.
2. **`constraint-dependencies` cannot express this.** Constraints can only **NARROW** an
   existing constraint set — they never widen or replace one. Testing feasibility with a
   constraint returns **UNSATISFIABLE as its EXPECTED output**, which reads exactly like
   proof that the upgrade is impossible. **It proves nothing.**
3. Neither of the above is evidence that the cap is real.

**`override-dependencies` is the correct instrument.** It *replaces* the offending
requirement rather than intersecting with it, which is precisely what a stale upstream cap
needs. It is a **judicious last resort**, and it is an **approved remedy** here — the
alternative (waiting for upstream) leaves a known-vulnerable pin shipping.

An override must **never be silent**. It forces a combination nobody tested, which is why
the written justification is the point, not paperwork. In `pyproject.toml`:

```toml
[tool.uv]
# OVERRIDE — PYSEC-2026-2132 (click <8.3.0). Capping package: inspect-ai (<8.3).
# Believed safe because: <upstream issue/PR showing the incompatibility was fixed>,
# and inspect-ai imports and its suite runs under click 8.3.x (verified <date>).
# REMOVE-WHEN: inspect-ai releases a version whose click cap admits >=8.3.
override-dependencies = ["click>=8.3.0"]
```

The justification must name **the advisory**, **the capping package**, and **why the
combination is believed safe**. Overriding a cap is only defensible when the cap is
**stale** rather than a real incompatibility, and that needs evidence: an upstream
issue/PR showing the incompatibility was fixed, plus the capped package actually importing
and running under the newer version.

`override-dependencies` is a workspace directive.
It is **not published in wheel metadata**, so it changes this repo's lock only —
downstream consumers of `nava-rebar` are unaffected.

### (c) Track the upstream fix so the override can be removed

Every override carries a **REMOVE-WHEN** condition, or it outlives its justification
silently. File a rebar ticket for the upstream fix and reference it beside the override.

The **staleness check is automated**: the scheduled dependency-advisory canary
(`.github/workflows/dependency-advisory-canary.yml`) re-audits `main`'s committed closure
daily. When the advisory clears — because upstream lifted the cap and the lock moved on —
the canary closes its alert ticket, which is the signal that the override's REMOVE-WHEN
condition has been met. Nobody has to remember to re-test.

## 5. Verification set for ANY lock intervention

Run all of these before pushing a lock change, in this order:

```sh
uv lock --check                 # the lock is consistent with pyproject (run under the uv versions in use)
uv sync --locked --extra dev    # the lock actually installs
pip-audit                       # over the WHOLE resulting environment
```

The final `pip-audit` over the whole environment is not redundant: it confirms the old pin
was **masking nothing else**. Moving one package moves its closure, and the version you
just unblocked can carry its own advisory.

## 6. Escalation — who gets told

The scheduled canary files **one** rebar bug tagged `dependency-advisory-alert` and keeps
it updated (at most one comment per 24h) for as long as the advisory is outstanding; it
closes the ticket when the advisory clears. The dedup is shared with the reconciler
heartbeat canary (`scripts/alert_dedup.py`), so a long-open advisory produces one ticket,
not one per daily run.

## Related

* `.github/workflows/_build-and-test.yml` — the per-change lane (Gerrit + branch)
* `.github/workflows/dependency-advisory-canary.yml` — the scheduled owner + escalation
* `.github/workflows/release.yml` — the release gate
* `scripts/dependency_audit.py` — the decision logic, unit-tested in
  `tests/scripts/test_dependency_audit.py`
