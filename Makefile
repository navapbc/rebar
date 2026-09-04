# rebar developer commands — the single source of truth for lint/format/type/test,
# mirrored 1:1 by CI and the pre-commit hook (so "what CI runs" is never a guess).
#
# Policy (modeled on Pydantic): MUTATION is opt-in and explicit — `make format` is the
# ONLY target that rewrites your files. Every automated gate (`make lint`, the
# pre-commit hook, CI) is CHECK-ONLY and never mutates, so it can fail loudly without
# reformatting code out from under you (or an agent mid-edit). The ruff version is
# pinned exactly in pyproject's [dev] extra, so all of these run the same ruff.

.DEFAULT_GOAL := help
# scripts/ is in scope (ticket ae96): seven of these files ARE the CI gates behind the
# `Verified` vote, so leaving the gate implementations themselves ungated was the hole.
sources = src tests scripts

# Pinned git-cliff (standalone Rust binary; install with `pipx install
# git-cliff==$(GIT_CLIFF_VERSION)`, NOT a pyproject dev extra). The `changelog`
# target refuses to run on a mismatched version so generated output is reproducible.
GIT_CLIFF_VERSION := 2.13.1

# Supply-chain lint (story 08a8; scope widened in epic 5664 S1): under `make lint`, zizmor
# (installed via the [dev] extra) audits release.yml + the Gerrit Verified-gate vote path (see
# ZIZMOR_WORKFLOWS below), and actionlint validates ALL workflows (bug 8002 — an invalid
# workflow that release.yml-only actionlint would miss took the reconcile bridge down for ~2d).
# actionlint is a standalone Go binary; when it is not already on PATH (CI ubuntu), the
# `actionlint-bin` target installs a PINNED version verified against a hard-coded SHA-256
# into a repo-local, git-ignored bin. Bump the pin + digest together (they are checked with
# `sha256sum -c --strict`, so a wrong digest fails the install loudly).
RELEASE_WORKFLOW := .github/workflows/release.yml
# Zizmor audit scope (epic 5664 S1; extended by ticket 1c70): the release workflow, the
# Gerrit Verified-gate vote-casting critical path — gerrit-verify.yaml (the workflow that
# casts the Verified vote) and ALL five reusables it calls that check out code / build
# artifacts (_build-and-test.yml, _mutation.yml, _optionality.yml, _artifact-probe.yml,
# _eval-discipline.yml) — PLUS reconcile-bridge.yml, the other privileged workflow in this
# repo: it runs with contents/actions-write and OIDC capability, so it carries the same
# action-security risk (pinning, credential-persistence, template-injection) as the vote
# path even though it does not itself cast a gate vote. Widening beyond release.yml closes
# the gap where the workflows that actually gate every change (or hold write/OIDC creds)
# were unaudited. Keep the set to these credentialed workflows (not all workflows) so the
# audit surface stays proportional to risk.
ZIZMOR_WORKFLOWS := $(RELEASE_WORKFLOW) \
	.github/workflows/gerrit-verify.yaml \
	.github/workflows/_build-and-test.yml \
	.github/workflows/_mutation.yml \
	.github/workflows/_optionality.yml \
	.github/workflows/_artifact-probe.yml \
	.github/workflows/_eval-discipline.yml \
	.github/workflows/reconcile-bridge.yml
ACTIONLINT_VERSION := 1.7.12
ACTIONLINT_SHA256_LINUX_AMD64 := 8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8
LOCAL_BIN := .tools/bin

# Dev interpreter pin (bug a5f5). Single-sourced in .github/python-version.txt — the same
# discipline as .github/git-version-floor.txt and .github/module-size-limit.txt — and held to
# the CI matrix by tests/unit/test_worktree_python_pin.py, so dropping this version from CI
# fails a test instead of silently leaving every fresh venv on an interpreter nothing tests.
PYTHON_VERSION_FILE := .github/python-version.txt

.PHONY: help install hooks amend-msg venv worktree format lint typecheck import-walk config-check check test jira-dc-up jira-dc-down vendor-security-rules changelog actionlint-bin verify-mcp-pin

help:  ## Show the available targets.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Install rebar from the committed uv.lock (uv sync --locked, dev + metrics extras) + the pre-commit hook (the commit gate).
	@# uv-canonical (ticket ce5d): the repo's own envs install THROUGH the committed lock so
	@# every checkout gets the same verified-importable dependency set (the unlocked pip path
	@# once resolved an import-broken pydantic-ai-slim/anthropic pair). `--locked` refuses to
	@# resolve fresh — a drifted lock fails here exactly like CI's `uv lock --check` gate.
	@#
	@# `--extra metrics` (ticket fd30) pins the code-health analyzer stack — lizard, which
	@# `rebar metrics` needs for its complexity lens — to the DEV BOOTSTRAP's own intent.
	@# It is behaviour-preserving today: the `dev` extra self-references
	@# `nava-rebar[agents,metrics]`, so lizard already arrives transitively. But that
	@# self-reference exists to make optional-capability TESTS run in CI, not to equip a
	@# developer's venv; naming the extra here means trimming that CI-motivated
	@# self-reference can no longer silently regress `rebar metrics` to `unavailable`
	@# locally. It adds no lock churn — the metrics extra is already in the committed lock.
	@# scc (Go) and jscpd (Node) cannot come from a Python extra; they are optional and
	@# documented in docs/local-dev-env.md, and their absence degrades to `unavailable`.
	@command -v uv >/dev/null 2>&1 || { \
		echo "ERROR: 'uv' is required (canonical bootstrap installs through the committed uv.lock)."; \
		echo "       Install it: https://docs.astral.sh/uv/getting-started/installation/"; \
		echo "       Unlocked fallback (resolves fresh — NOT the canonical env): make install-unlocked"; \
		exit 1; }
	uv sync --locked --extra dev --extra metrics
	$(MAKE) hooks

install-unlocked:  ## Fallback: editable pip install (UNLOCKED — resolves fresh; prefer `make install`).
	@# `metrics` named explicitly for the same reason as in `install` above — the dev
	@# extra's `nava-rebar[agents,metrics]` self-reference is CI-motivated, so this states
	@# the dev-env requirement itself rather than inheriting it by coincidence.
	python -m pip install -e '.[dev,metrics]'
	$(MAKE) hooks

hooks:  ## (Re)install the pre-commit git hook and VERIFY it landed (the commit gate).
	@# pre-commit refuses to install when core.hooksPath is set (it fails loudly with
	@# "Cowardly refusing..."). A value pointing at the DEFAULT hooks dir is redundant and
	@# safe to unset; any OTHER value is a deliberate setup we must not clobber — guide and
	@# stop. Then install and VERIFY the hook file exists, so the gate is never silently
	@# absent (the failure mode that let a format error reach CI).
	@# Installed WITHOUT -f/--overwrite on purpose: `default_install_hook_types` adds the
	@# commit-msg hook (the 50/72 message gate), and Gerrit's own commit-msg hook (which
	@# stamps Change-Id) already owns that slot. pre-commit's default migration mode
	@# preserves it as commit-msg.legacy and runs BOTH; -f would delete it and silently
	@# break every push to Gerrit. Both hooks AND the Change-Id chain are verified below.
	@hp="$$(git config --get core.hooksPath || true)"; \
	common="$$(git rev-parse --git-common-dir)"; \
	if [ -n "$$hp" ]; then \
		if [ "$$hp" = "$$common/hooks" ] || [ "$$hp" = ".git/hooks" ]; then \
			echo "note: unsetting redundant local core.hooksPath ($$hp = git default)"; \
			git config --unset-all core.hooksPath || true; \
		else \
			echo "ERROR: core.hooksPath is set to '$$hp' — pre-commit cannot install the hook."; \
			echo "       It looks deliberate, so 'make hooks' will not change it. To use the"; \
			echo "       pre-commit gate, unset it (scope-appropriately) then re-run 'make hooks':"; \
			echo "         git config --unset-all core.hooksPath          # if set locally"; \
			echo "         git config --global --unset-all core.hooksPath  # if set globally"; \
			exit 1; \
		fi; \
	fi; \
	pre-commit install; \
	sh scripts/install-gerrit-hook.sh || true; \
	hook="$$common/hooks/pre-commit"; \
	msg_hook="$$common/hooks/commit-msg"; \
	if [ -f "$$hook" ]; then \
		echo "✓ commit gate active: pre-commit hook installed at $$hook"; \
	else \
		echo "ERROR: pre-commit hook NOT found at $$hook after install — the commit gate is NOT active."; \
		exit 1; \
	fi; \
	if [ -f "$$msg_hook" ]; then \
		echo "✓ message gate active: commit-msg hook installed at $$msg_hook"; \
	else \
		echo "ERROR: commit-msg hook NOT found at $$msg_hook — the 50/72 message gate is NOT active."; \
		exit 1; \
	fi; \
	if grep -q "Change-Id" "$$msg_hook" 2>/dev/null; then \
		echo "✓ Gerrit Change-Id stamping preserved (in the commit-msg hook itself)"; \
	elif [ -f "$$msg_hook.legacy" ] && grep -q "Change-Id" "$$msg_hook.legacy" 2>/dev/null; then \
		echo "✓ Gerrit Change-Id stamping preserved (chained as commit-msg.legacy)"; \
	else \
		echo "WARNING: no Change-Id stamping found in $$msg_hook or its .legacy chain."; \
		echo "         Pushes to Gerrit will be rejected without a Change-Id. The install"; \
		echo "         above failed (offline? host unreachable?) — re-run once you have"; \
		echo "         network access to the Gerrit host:"; \
		echo "           make hooks     # -> scripts/install-gerrit-hook.sh"; \
		echo "         Do NOT curl over \$$(git rev-parse --git-path hooks/commit-msg):"; \
		echo "         from a linked worktree that clobbers the SHARED pre-commit wrapper."; \
	fi

amend-msg:  ## Amend HEAD with FILE's commit message, CARRYING FORWARD its Change-Id. Usage: make amend-msg FILE=<path>
	@# Ticket 5304. `git commit --amend -F/-m` REPLACES the whole message, dropping the
	@# Change-Id; Gerrit's commit-msg hook then stamps a FRESH one and the next push opens a
	@# DUPLICATE change instead of a patchset (Gerrit 1921, 1926, 1931 in one session). No
	@# hook can catch it — an `-F` amend reaches prepare-commit-msg as source='message' with
	@# an EMPTY sha, indistinguishable from a fresh `-F` commit — so, like Go's `git
	@# codereview change` / OpenStack's `git-review` / `repo upload` / `git cl upload`, the
	@# remedy is a wrapper that makes the failure unreachable. Use `git commit --amend
	@# --no-edit` when the message is UNCHANGED; use this when you are REWRITING it.
	@if [ -z "$(FILE)" ]; then \
		echo "usage: make amend-msg FILE=<path>"; \
		echo "  Amends HEAD with the commit message in <path>, carrying HEAD's existing"; \
		echo "  Change-Id forward. Fails loudly if HEAD has no Change-Id (run 'make hooks')."; \
		exit 2; \
	fi
	python scripts/amend_commit_message.py "$(FILE)"

venv:  ## Create .venv on the CI-pinned interpreter ($(PYTHON_VERSION_FILE)). Fails loudly rather than using ambient python3.
	@# Bug a5f5: this step used to be `python3 -m venv .venv`, inheriting whatever the host's
	@# ambient python3 resolved to — 3.14.6 on the machine where the bug was found, while CI
	@# tested 3.11/3.12/3.13. requires-python is only ">=3.11", so `uv sync --locked` accepted
	@# the mismatch and it stayed silent. Every worktree made by `make worktree` therefore ran
	@# an interpreter CI never exercises, producing local failures CI could not reproduce (and
	@# hiding real ones behind "probably just my env").
	@#
	@# So: ask for the pinned version explicitly, and treat its absence as an ERROR. A silent
	@# fallback to the ambient interpreter is exactly the defect, not a graceful degradation.
	@command -v uv >/dev/null 2>&1 || { \
		echo "ERROR: 'uv' is required to provision a version-pinned venv."; \
		echo "       Install it: https://docs.astral.sh/uv/getting-started/installation/"; \
		exit 1; }
	@python_version="$$(tr -d '[:space:]' < $(PYTHON_VERSION_FILE))"; \
	echo "→ uv venv --python $$python_version .venv   (pinned by $(PYTHON_VERSION_FILE))"; \
	uv venv --python "$$python_version" .venv || { \
		echo ""; \
		echo "ERROR: could not provision .venv on Python $$python_version."; \
		echo "       That version is what CI tests ($(PYTHON_VERSION_FILE)); this host's"; \
		echo "       ambient python3 is $$(python3 --version 2>&1 || echo 'not installed')."; \
		echo "       Refusing to fall back to it — a venv on an untested interpreter yields"; \
		echo "       local failures CI cannot reproduce, and masks the ones that matter."; \
		echo "       Let uv fetch the right one:  uv python install $$python_version"; \
		exit 1; }

worktree:  ## Create a fresh worktree from origin/main + provision its venv & hooks. Usage: make worktree name=<branch> [dir=<path>]
	@# One-command form of the manual setup the repo mandates (fresh worktree branched
	@# from current origin/main, with its OWN local venv + the commit gate wired) — so
	@# agents/humans stop hand-running the sequence. Delegates to the canonical `make
	@# install` for provisioning rather than forking its steps. The Gerrit commit-msg
	@# hook needs no re-fetch: worktrees share the common git dir's hooks, so a worktree
	@# created from this checkout inherits it automatically.
	@if [ -z "$(name)" ]; then \
		echo "usage: make worktree name=<branch> [dir=<path>]"; \
		echo "  Creates a worktree at <path> (default ../<branch>) branched from a freshly-"; \
		echo "  fetched origin/main, then provisions .venv + editable install + the pre-commit gate."; \
		exit 2; \
	fi
	@target_dir="$(if $(dir),$(dir),../$(name))"; \
	echo "→ git fetch origin"; \
	git fetch origin; \
	echo "→ git worktree add $$target_dir -b $(name) origin/main"; \
	git worktree add "$$target_dir" -b "$(name)" origin/main; \
	echo "→ provisioning $$target_dir/.venv (pinned Python) then canonical 'make install'"; \
	( cd "$$target_dir" && $(MAKE) venv && . .venv/bin/activate && $(MAKE) install ); \
	echo ""; \
	echo "✓ worktree ready: $$target_dir"; \
	echo "  activate it with:  cd $$target_dir && source .venv/bin/activate"

format:  ## MUTATES: auto-fix lint + format the code (the ONLY rewriting target).
	ruff check --fix $(sources)
	ruff format $(sources)

lint:  ## ERRORS ONLY (never mutates): ruff lint + format-check + zizmor (release.yml + Verified-gate vote path) + actionlint (all workflows) + DCO identity consistency. The gate CI runs.
	ruff check $(sources)
	ruff format --check $(sources)
	@# Shrink-only function-complexity ratchet (story c9f7): C901 over src/rebar only,
	@# threshold from [tool.ruff.lint.mccabe] in pyproject.toml, compared against the
	@# checked-in .github/complexity-baseline.json. Fails on new/increased complexity;
	@# the ruff/format checks above still cover both src and tests.
	python scripts/check_complexity_baseline.py --check
	@# Shrink-only MECHANISM-delta ratchet (ticket 9ca8-675e-4dfb-427d,
	@# unblacked-loveless-toad). The defect it prevents: 56% of sampled fixes ADD a mechanism
	@# — a lock, a knob, an env var, a gate script, an autouse fixture, a test helper, a
	@# feature flag — against 30% that are pure logic fixes, so each cycle grows the very
	@# surface that produces the next cycle's defect classes, and nothing pushed back. This
	@# compares the live per-(kind, name) census against .github/mechanism-baseline.json and
	@# fails on a new mechanism that carries no in-tree `# mechanism-ok: <kind> <name> —
	@# <reason>` justification; REMOVING one is always allowed (it buckets as stale), which is
	@# what makes it a ratchet rather than a freeze. Stdlib + PyYAML (a dev dep, read-only),
	@# so it runs identically here, in a pre-commit hook, or on a checkout with no CI provider
	@# at all (project.portability) — CI inherits it through this `make lint` step.
	python scripts/check_mechanism_delta.py --check
	@# Config-ownership + field-consumption gates (RP-04 S7.2, ticket 735b): the portable,
	@# no-CI-required trigger for both config-boundary gates. CI inherits them via this
	@# `make lint` step, so neither is a standalone CI step (no double-run). A patchset
	@# predating this slice has a Makefile without these lines, so the tree-skew case needs
	@# no if-present guard — `make lint` runs the patchset's own Makefile.
	python scripts/check_config_ownership.py
	python scripts/check_config_reads.py
	@# Env-var registry drift (bug: fail-closed os.environ scan). CI runs this in the
	@# `Env-var registry drift gate` step of .github/actions/docs-gates/action.yml and via
	@# tests/interfaces/facades/test_mcp_http_transport.py; running it here makes the local
	@# verdict agree with CI instead of surfacing staleness only in the full suite (~0.55s).
	python scripts/gen_env_registry.py --check
	@# MCP-tool-surface vs library-facade parity gate (ticket 8ce5-b870-601d-4715,
	@# cream-capitate-snake). The MCP tools are thin closures over the `rebar.__all__`
	@# facade, but nothing cross-checked the two: a library function could gain, lose or
	@# rename a parameter while its MCP tool kept the old shape and every gate stayed green
	@# — gen_mcp_reference.py documents only what the registrars expose and never looks at
	@# the library. This compares both live surfaces against the committed manifest
	@# (tests/unit/mcp_library_parity_manifest.json) and fails on drift; an intentional
	@# difference is declared IN the manifest with a non-empty `reason`, so justified
	@# divergence passes and silent divergence does not. Regenerate with
	@# `python scripts/check_mcp_library_parity.py --update`. Pure introspection of rebar's
	@# own package + the `mcp` dependency — no CI provider required (project.portability),
	@# and it FAILS rather than skips if the import is unavailable.
	python scripts/check_mcp_library_parity.py
	@# Comment-hygiene gate (ticket 2d9a-78c5): CI runs this as the `comment-hygiene gate`
	@# step of _build-and-test.yml and via tests/unit/test_comment_hygiene_guard.py; running
	@# it here makes the local verdict agree with CI instead of surfacing findings only in
	@# the full suite — same rationale as the env-var drift gate above (~4.9s on the full tree).
	python scripts/check_comment_hygiene.py
	@# Test-hygiene gates (story bold-abeyant-indri). Both scripts already run in CI
	@# (_build-and-test.yml, `raw-git-write gate` + `wall-clock-assert gate`) but were
	@# unreachable from `make lint`, so a local verdict could be green while CI was red —
	@# the same reachability gap task 2d9a-78c5 closed for the comment-hygiene gate above.
	@# No `if [ -f ]` guard is needed here (unlike the CI steps, which must tolerate a
	@# patchset predating the script): `make lint` runs the patchset's OWN Makefile.
	python scripts/check_raw_git_writes.py
	python scripts/check_wall_clock_asserts.py
	python scripts/check_bare_repository_discovery.py
	@# Destructive test-exec gate (ticket 6818-615f-555e-4bb9). A test must not
	@# subprocess-exec a shell script whose deletion target is an unguarded variable
	@# interpolation: on 2026-08-26 exactly that ran `rm -rf "$${dir}"/*` with dir="",
	@# the glob expanded to `rm -rf /*`, and it destroyed /opt/homebrew and the
	@# Homebrew-installed apps in /Applications before a 60s timeout stopped it.
	@# Two shapes clear the gate: an injectable seam (`"$${RM_CMD:-rm}"`) or a
	@# `: "$${dir:?}"` abort guard. `set -u` does NOT clear it — it fires on unset,
	@# not set-but-empty, and set-but-empty is what the incident had. Static analysis
	@# only: it cannot stop a mutation at runtime (see ticket e668-b496-e264-4283 for
	@# the OS sandbox), so it is defence in depth, not the control.
	python scripts/check_destructive_test_exec.py
	@# Tickets-store boundary gate (bug 0514-92e0-e6c4-4304). The store is RELOCATABLE
	@# (`REBAR_TRACKER_DIR`, or an absolute `tracker.dir` — EV-3b), but 13 shipped call
	@# sites composed `repo_root / ".tickets-tracker"` instead of resolving it, so the
	@# deployed MCP server's `bridge_status` read a directory nobody configured while the
	@# same server served 2763 tickets from the real store. Flags PATH COMPOSITION only —
	@# docstrings, comments, error text and argparse help are untouched — and sanction is
	@# `# tickets-boundary-ok: <reason>`, the reason MANDATORY: the bare marker was already
	@# a documented convention that nothing enforced, and 7 of the 13 defects carried one.
	python scripts/check_tickets_boundary.py
	@# Repo/config root derived from the store (bug auspicial-friended-merganser): a relocated
	@# store (REBAR_TRACKER_DIR) makes `os.path.dirname(tracker)` a directory with no
	@# rebar.toml, so config reads there resolve an EMPTY config — that silently disabled the
	@# transition-open->in_progress plan-review gate on the deployed MCP server. Flags the
	@# `dirname(<tracker>)` composition only; sanction is `# repo-root-ok: <reason>` (reason
	@# MANDATORY), used for the one detached-child site whose cwd IS the store.
	python scripts/check_repo_root_from_tracker.py
	@# Repo root derived from the PACKAGE LOCATION (bug impressive-doddering-alpinegoat, c0b9):
	@# `Path(__file__).resolve().parents[N]` is the checkout ONLY under an editable install;
	@# under a wheel install it climbs into site-packages, so the reconciler silently resolved
	@# repo_root=<venv>/lib/pythonX. Flags the `Path(__file__).parents[...]` subscript root-climb
	@# only (the singular `.parent` package-data idiom is untouched); sanction is
	@# `# pkg-root-ok: <reason>` / `# pkg-root-seam: <reason>` (reason MANDATORY).
	python scripts/check_repo_root_from_package.py
	@# CLI --output json JS-safe-integer gate (bug unhelping-creviced-rhino, e127-a3ad-895a-4a2f).
	@# rebar's 19-digit nanosecond timestamps are outside the RFC 8259 §6 interoperable range,
	@# so a bare JSON number on a CLI --output json stream is silently rounded by float64
	@# consumers (jq/Node/Ruby, a measured -42 ns drift) and breaks BigInt consumers (GitHub
	@# Copilot CLI: `TypeError: Do not know how to serialize a BigInt`). Change 2347 introduced
	@# the js_safe_dumps choke point and converted the primary emitters; this gate makes the
	@# single-choke-point invariant enforceable — it flags a stdout write built by a RAW
	@# json.dumps on the CLI surface so a future emitter cannot reintroduce a bare big-int.
	@# Sanction is `# js-safe-ok: <reason>` (reason MANDATORY) for a write that carries no ns ts.
	python scripts/check_cli_json_js_safe.py
	@# DCO sign-off identity consistency (story 35d2): contributor-facing guidance must not
	@# hardcode a personal sign-off identity; automation-owned paths are excluded by the script.
	python scripts/check_dco_identity.py
	python scripts/check_criteria_vocabulary.py
	@# Agent Skills SKILL.md frontmatter (ticket db04): Copilot CLI silently drops a skill
	@# whose frontmatter fails to parse or whose description exceeds 1024 chars, so gate it.
	python scripts/check_skill_frontmatter.py
	@# POSIX-only-import collection guard (bug infamous-protected-baboon, 0b31-aeb5-e734-41c9):
	@# fcntl has no Windows build, so an UNCONDITIONAL module-scope `import fcntl` makes the
	@# module — and every importer, incl. `import rebar` via _commands.doctor_locks — fail to
	@# COLLECT off POSIX (the Windows sweep tier went fully red). Flags a module-scope fcntl
	@# import not made conditional by a `try` or `if` platform guard; lazy in-function imports
	@# are ignored. Sanction is `# fcntl-guard-ok: <reason>` (reason MANDATORY). Stdlib-only,
	@# no CI provider required (project.portability).
	python scripts/check_fcntl_import_guard.py
	@# ShellCheck over standalone *.sh (ticket fe4e-54a5-3c3a-4901). Workflow `run:` blocks
	@# are ALREADY linted — actionlint (below) embeds ShellCheck for .github/workflows/** —
	@# but no gate covered standalone scripts, of which this repo has 35.
	@# Severity is `warning`, and that is load-bearing rather than incidental: the motivating
	@# defect is SC2115 (`rm -rf "$$dir"/*` expanding to `rm -rf /*` when the var is empty),
	@# which ShellCheck emits at `warning`. A gate at `-S error` runs GREEN over that exact
	@# line — the one that wiped /opt/homebrew and /Applications on a contributor workstation.
	@# ShellCheck ships via `shellcheck-py`, pinned exactly in pyproject's [dev] extra, so it
	@# is a REQUIRED tool here and the gate FAILS (never skips) when it is absent.
	python scripts/check_shellcheck.py
	@# Deploy-manifest completeness gate (ticket 0a6a-04d3-8fd9-4cd5). autodeploy.sh's
	@# hand-curated *_PATHS lists have silently drifted from reality four times (a
	@# deploy-relevant infra file added but never listed, so a later change reaches the box
	@# with no signal). This DERIVES the expected path set from Dockerfile/compose COPY/
	@# install/ENTRYPOINT refs + materialize-*.sh / *-entrypoint.sh / compose-up.sh globs and
	@# fails on any derived path in NO manifest — the same derive-and-diff shape as
	@# check_server_manifest.py. Stdlib only, no CI provider required (portable), so `make
	@# lint` and the pre-commit hook both enforce it.
	python scripts/check_deploy_manifest.py
	@# templatefile() escape gate (bug dd30-f10d-69f3-4c36). Sibling to the ShellCheck gate
	@# above, and deliberately NOT covered by it: `templatefile()` interpolates the WHOLE
	@# template — comments included, because `#` means nothing to it — so an unescaped
	@# `$${...}` in a COMMENT is parsed as HCL. ShellCheck reads the same line as valid bash
	@# and passes, which is exactly how commit ef1a7e66a65d broke EVERY terraform operation
	@# in the repo (`-target` included: terraform evaluates the whole configuration first).
	@# The rule is declared-variable-aware, not a blanket ban on `$${`: user_data.sh has four
	@# legitimate `$${data_volume_id}` references, the one variable main.tf passes.
	python scripts/check_templatefile_escapes.py
	@# SSM SecureString secret-in-state gate (bug eb67-b96c-dcf0-4f86, ADR 0105). Sibling to the
	@# templatefile gate above and structured the same way. A SecureString secret written with a
	@# plaintext `value` persists that value in CLEARTEXT in the remote terraform state — the
	@# provider reads the live value into state on every refresh even under `ignore_changes =
	@# [value]`. This gate fails the build unless every operator-seeded SecureString secret uses
	@# write-only `value_wo` + `value_wo_version` (never stored to state). Pure-Python + hermetic
	@# (no live AWS), so it is portable across CI providers.
	python scripts/check_ssm_secret_state.py
	@# Single-source uv pin (bug 56b7-b21a-c8ab-4afc). setup-uv is SHA-pinned at all 25 call
	@# sites, but uv ITSELF was not, so the action fell back to fetching a remote manifest on
	@# every job -- and when that fetch failed the job failed with `##[error]fetch failed`, a
	@# verdict unrelated to the change under test (run 33214025855, dead in 21s). The pin is one
	@# line, `[tool.uv] required-version` in pyproject.toml; this gate keeps it the SINGLE source
	@# by rejecting the four ways it can be defeated -- removed, loosened to a range (which still
	@# fetches, so it reads as pinned while restoring the outage), overridden by a per-call-site
	@# `version:`/`version-file:` input (resolved AHEAD of the pyproject scan), or shadowed by a
	@# root uv.toml. Stdlib + PyYAML, no CI provider required -- same portability contract as the
	@# ShellCheck and templatefile gates above.
	python scripts/check_uv_pin.py
	@# Release supply-chain audits (story 08a8), AFTER ruff so ruff findings still surface.
	@# zizmor audits the release workflow + the Verified-gate vote path ($(ZIZMOR_WORKFLOWS),
	@# widened in epic 5664 S1); actionlint below validates ALL workflows. zizmor is a
	@# cross-platform pip tool (in [dev]).
	@# Online/offline split (bug 7a03): with NO GitHub token in the environment zizmor drops to
	@# offline mode and prints a "running in offline mode" WARN while skipping its five
	@# token-backed audits (impostor-commit, ref-confusion, known-vulnerable-actions,
	@# stale-action-refs, ref-version-mismatch). Local `make lint` stays portable/offline, so
	@# pass `--offline` EXPLICITLY there to drop that misleading warning from a clean lint. CI
	@# (Gerrit Verify + the push/PR lanes) sets GH_TOKEN, so the same step runs ONLINE and the
	@# five live-metadata audits execute. Detect a token here rather than in CI so both invoke
	@# one Makefile — the portability contract (an offline local fallback, online in CI).
	@if [ -n "$${GH_TOKEN:-$${GITHUB_TOKEN:-$${ZIZMOR_GITHUB_TOKEN:-}}}" ]; then \
		zizmor $(ZIZMOR_WORKFLOWS); \
	else \
		zizmor --offline $(ZIZMOR_WORKFLOWS); \
	fi
	@# actionlint validates ALL workflows — the context-availability / parse-error class (e.g.
	@# the reconcile-bridge `runner`-in-job-env startup failure, bug 8002) that release.yml-only
	@# linting missed. It is an OS/arch-specific Go binary: use one already on PATH / in .tools;
	@# else install the PINNED build — but ONLY on Linux, where the pinned linux_amd64 asset runs
	@# and GNU sha256sum verifies it. On a non-Linux host WITHOUT actionlint (e.g. the macOS CI
	@# matrix leg), skip with a notice — the Linux CI leg is the gating run, so coverage is not
	@# lost. Given no path args, actionlint auto-discovers every .github/workflows/*.{yml,yaml}.
	@al="$$(command -v actionlint || echo $(LOCAL_BIN)/actionlint)"; \
	if [ -x "$$al" ]; then \
		echo "$$al (all workflows)"; "$$al"; \
	elif [ "$$(uname -s)" = "Linux" ]; then \
		$(MAKE) actionlint-bin; echo "$(LOCAL_BIN)/actionlint (all workflows)"; "$(LOCAL_BIN)/actionlint"; \
	else \
		echo "lint: actionlint unavailable on $$(uname -s); skipping workflow actionlint audit (gated on the Linux CI leg)"; \
	fi

actionlint-bin:  ## Ensure a pinned actionlint is available (repo-local, digest-verified install if absent).
	@if command -v actionlint >/dev/null 2>&1; then \
		echo "actionlint: using $$(command -v actionlint)"; \
	elif [ -x "$(LOCAL_BIN)/actionlint" ]; then \
		echo "actionlint: using $(LOCAL_BIN)/actionlint"; \
	else \
		set -e; \
		echo "actionlint not found — installing pinned v$(ACTIONLINT_VERSION) into $(LOCAL_BIN)"; \
		mkdir -p "$(LOCAL_BIN)"; \
		tmp="$$(mktemp -d)"; \
		trap 'rm -rf "$$tmp"' EXIT; \
		url="https://github.com/rhysd/actionlint/releases/download/v$(ACTIONLINT_VERSION)/actionlint_$(ACTIONLINT_VERSION)_linux_amd64.tar.gz"; \
		curl --retry 3 --retry-delay 2 --retry-all-errors -fsSL "$$url" -o "$$tmp/actionlint.tar.gz"; \
		echo "$(ACTIONLINT_SHA256_LINUX_AMD64)  $$tmp/actionlint.tar.gz" | sha256sum -c --strict; \
		tar -C "$(LOCAL_BIN)" -xzf "$$tmp/actionlint.tar.gz" actionlint; \
		echo "actionlint: installed $(LOCAL_BIN)/actionlint"; \
	fi

verify-mcp-pin:  ## Verify the embedded mcp-publisher SHA-256 matches the live pinned download.
	python scripts/verify_mcp_publisher_pin.py

# scripts/ is IN the typecheck scope (ticket cc99). ae96 brought scripts/ under ruff
# but not mypy, leaving the CI gate implementations themselves type-unchecked even
# though several of them ARE the gates behind the `Verified` vote. Including them cost
# 15 fixes across 6 files (all missing annotations or lost `X | None` narrowing, several
# latent bugs) — no per-module mypy override and no blanket ignores were needed, because
# pyproject's `follow_imports = "silent"` + `ignore_missing_imports` already absorb the
# `sys.path` bootstraps ae96 flagged as the structural obstacle.
typecheck:  ## ERRORS ONLY: mypy over the whole library + scripts/ (gating).
	mypy src/rebar scripts

import-walk:  ## ERRORS ONLY: deterministic import walk — every rebar.* module + each scripts/*.py standalone (ticket 37b9; same check the CI wheel probe runs).
	python scripts/check_import_walk.py

config-check:  ## ERRORS ONLY: validate every infra config (fails CI on a malformed config -> can't reach main).
	bash infra/scripts/config-check.sh

check: lint typecheck  ## Run every check-only gate (no mutation).

test:  ## Run the default test suite (excludes integration + external).
	pytest -m "not integration and not external" -q

jira-dc-up:  ## Build + start the Jira DC verification harness (fresh instance; see tests/external/live_jira_dc/README.md).
	cd tests/external/live_jira_dc && docker compose up -d --build --force-recreate

jira-dc-down:  ## Stop + remove the Jira DC verification harness.
	cd tests/external/live_jira_dc && docker compose down -v

changelog:  ## Prepend the unreleased CHANGELOG.md section for a release: make changelog VERSION=vX.Y.Z (generate-then-curate; never a full regen).
	@command -v git-cliff >/dev/null 2>&1 || { echo "error: git-cliff not installed — run: pipx install git-cliff==$(GIT_CLIFF_VERSION)"; exit 1; }
	@have="$$(git-cliff --version | awk '{print $$2}')"; \
	 if [ "$$have" != "$(GIT_CLIFF_VERSION)" ]; then \
	   echo "error: git-cliff $$have does not match the pin $(GIT_CLIFF_VERSION) — run: pipx install git-cliff==$(GIT_CLIFF_VERSION)"; exit 1; \
	 fi
	@if [ -z "$(VERSION)" ]; then echo "error: VERSION is required, e.g. make changelog VERSION=v0.8.0"; exit 1; fi
	@ver="$$(printf '%s' '$(VERSION)' | sed 's/^v//')"; \
	 if grep -q "^## \[$$ver\]" CHANGELOG.md; then \
	   echo "CHANGELOG.md already has a [$$ver] section — nothing to do (idempotent; re-run is a no-op)."; \
	 else \
	   git cliff --unreleased --tag $(VERSION) --prepend CHANGELOG.md && \
	   echo "Prepended the [$$ver] section — now HAND-CURATE the top block before committing and tagging."; \
	 fi

# epic b744 / WS5: refresh the VENDORED, PINNED High/Critical security rule subset
# (src/rebar/grounding/detectors/builtin/security_*.yaml). The rules are vendored (not a live
# registry pull) for reproducible/offline scanning, so they must be refreshed on a cadence
# (target: quarterly, or when a relevant CVE/rule family lands) via a deliberate PR — see
# docs/adr/0012. This target prints the refresh procedure + the pinned families (a real
# auto-pull is intentionally NOT wired: vendoring is a reviewed, pinned change, not a silent
# live fetch). The companion CI freshness check is `python -m rebar.grounding.detectors.security_pin`
# (the "Security-rules freshness gate" step in .github/workflows/test.yml): it WARNS when the
# `vendored_at` pin in security_rules_pin.json is older than the quarterly cadence. (Time-based +
# network-free; an upstream-version diff is the documented follow-on — see docs/adr/0012.)
vendor-security-rules:  ## Print how to refresh the vendored security rule subset (WS5).
	@echo "Vendored security rule families (refresh on the docs/adr/0012 cadence):"
	@echo "  - p/owasp-top-ten subset  -> security_owasp_cwe.yaml"
	@echo "  - p/cwe-top-25 subset     -> security_owasp_cwe.yaml"
	@echo "  - gitleaks (secrets)      -> security_secrets_gitleaks.yaml (sentinel; rules in the binary)"
	@echo "Refresh: review upstream for new High/Critical rules, port the curated subset to the"
	@echo "above YAML as native opengrep rules (rebar.builtin.security.* ids + rebar_envelope),"
	@echo "validate with 'opengrep scan --validate', then BUMP \`vendored_at\` in"
	@echo "security_rules_pin.json (resets the CI freshness gate) and open a PR pinning the snapshot."
	@echo ""
	@echo "Current freshness:"
	@python -m rebar.grounding.detectors.security_pin || true
