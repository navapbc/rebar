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

# Release supply-chain lint (story 08a8): under `make lint`, zizmor (installed via the [dev]
# extra) audits release.yml, and actionlint validates ALL workflows (bug 8002 — an invalid
# workflow that release.yml-only actionlint would miss took the reconcile bridge down for ~2d).
# actionlint is a standalone Go binary; when it is not already on PATH (CI ubuntu), the
# `actionlint-bin` target installs a PINNED version verified against a hard-coded SHA-256
# into a repo-local, git-ignored bin. Bump the pin + digest together (they are checked with
# `sha256sum -c --strict`, so a wrong digest fails the install loudly).
RELEASE_WORKFLOW := .github/workflows/release.yml
ACTIONLINT_VERSION := 1.7.12
ACTIONLINT_SHA256_LINUX_AMD64 := 8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8
LOCAL_BIN := .tools/bin

# Dev interpreter pin (bug a5f5). Single-sourced in .github/python-version.txt — the same
# discipline as .github/git-version-floor.txt and .github/module-size-limit.txt — and held to
# the CI matrix by tests/unit/test_worktree_python_pin.py, so dropping this version from CI
# fails a test instead of silently leaving every fresh venv on an interpreter nothing tests.
PYTHON_VERSION_FILE := .github/python-version.txt

.PHONY: help install hooks venv worktree format lint typecheck config-check check test jira-dc-up jira-dc-down vendor-security-rules changelog actionlint-bin verify-mcp-pin

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

lint:  ## ERRORS ONLY (never mutates): ruff lint + format-check + zizmor (release.yml) + actionlint (all workflows) + DCO identity consistency. The gate CI runs.
	ruff check $(sources)
	ruff format --check $(sources)
	@# Shrink-only function-complexity ratchet (story c9f7): C901 over src/rebar only,
	@# threshold from [tool.ruff.lint.mccabe] in pyproject.toml, compared against the
	@# checked-in .github/complexity-baseline.json. Fails on new/increased complexity;
	@# the ruff/format checks above still cover both src and tests.
	python scripts/check_complexity_baseline.py --check
	@# Config-ownership + field-consumption gates (RP-04 S7.2, ticket 735b): the portable,
	@# no-CI-required trigger for both config-boundary gates. CI inherits them via this
	@# `make lint` step, so neither is a standalone CI step (no double-run). A patchset
	@# predating this slice has a Makefile without these lines, so the tree-skew case needs
	@# no if-present guard — `make lint` runs the patchset's own Makefile.
	python scripts/check_config_ownership.py
	python scripts/check_config_reads.py
	@# DCO sign-off identity consistency (story 35d2): contributor-facing guidance must not
	@# hardcode a personal sign-off identity; automation-owned paths are excluded by the script.
	python scripts/check_dco_identity.py
	python scripts/check_criteria_vocabulary.py
	@# Agent Skills SKILL.md frontmatter (ticket db04): Copilot CLI silently drops a skill
	@# whose frontmatter fails to parse or whose description exceeds 1024 chars, so gate it.
	python scripts/check_skill_frontmatter.py
	@# Release supply-chain audits (story 08a8), AFTER ruff so ruff findings still surface.
	@# zizmor stays scoped to release.yml (widening the security audit is separate work);
	@# actionlint below validates ALL workflows. zizmor is a cross-platform pip tool (in [dev]).
	zizmor $(RELEASE_WORKFLOW)
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
