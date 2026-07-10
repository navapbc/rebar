# rebar developer commands — the single source of truth for lint/format/type/test,
# mirrored 1:1 by CI and the pre-commit hook (so "what CI runs" is never a guess).
#
# Policy (modeled on Pydantic): MUTATION is opt-in and explicit — `make format` is the
# ONLY target that rewrites your files. Every automated gate (`make lint`, the
# pre-commit hook, CI) is CHECK-ONLY and never mutates, so it can fail loudly without
# reformatting code out from under you (or an agent mid-edit). The ruff version is
# pinned exactly in pyproject's [dev] extra, so all of these run the same ruff.

.DEFAULT_GOAL := help
sources = src tests

# Pinned git-cliff (standalone Rust binary; install with `pipx install
# git-cliff==$(GIT_CLIFF_VERSION)`, NOT a pyproject dev extra). The `changelog`
# target refuses to run on a mismatched version so generated output is reproducible.
GIT_CLIFF_VERSION := 2.13.1

.PHONY: help install hooks format lint typecheck config-check check test vendor-security-rules changelog

help:  ## Show the available targets.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Install rebar (editable) + dev deps + the pre-commit hook (the commit gate).
	python -m pip install -e '.[dev]'
	$(MAKE) hooks

hooks:  ## (Re)install the pre-commit git hook and VERIFY it landed (the commit gate).
	@# pre-commit refuses to install when core.hooksPath is set (it fails loudly with
	@# "Cowardly refusing..."). A value pointing at the DEFAULT hooks dir is redundant and
	@# safe to unset; any OTHER value is a deliberate setup we must not clobber — guide and
	@# stop. Then install and VERIFY the hook file exists, so the gate is never silently
	@# absent (the failure mode that let a format error reach CI).
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
	hook="$$common/hooks/pre-commit"; \
	if [ -f "$$hook" ]; then \
		echo "✓ commit gate active: pre-commit hook installed at $$hook"; \
	else \
		echo "ERROR: pre-commit hook NOT found at $$hook after install — the commit gate is NOT active."; \
		exit 1; \
	fi

format:  ## MUTATES: auto-fix lint + format the code (the ONLY rewriting target).
	ruff check --fix $(sources)
	ruff format $(sources)

lint:  ## ERRORS ONLY (never mutates): ruff lint + format-check. The gate CI runs.
	ruff check $(sources)
	ruff format --check $(sources)

typecheck:  ## ERRORS ONLY: mypy over the whole library (gating; full src/rebar).
	mypy src/rebar

config-check:  ## ERRORS ONLY: validate every infra config (fails CI on a malformed config -> can't reach main).
	bash infra/scripts/config-check.sh

check: lint typecheck  ## Run every check-only gate (no mutation).

test:  ## Run the default test suite (excludes integration + external).
	pytest -m "not integration and not external" -q

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
