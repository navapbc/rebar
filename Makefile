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

.PHONY: help install format lint typecheck check test

help:  ## Show the available targets.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Install rebar (editable) + dev deps + the pre-commit hook.
	python -m pip install -e '.[dev]'
	pre-commit install

format:  ## MUTATES: auto-fix lint + format the code (the ONLY rewriting target).
	ruff check --fix $(sources)
	ruff format $(sources)

lint:  ## ERRORS ONLY (never mutates): ruff lint + format-check. The gate CI runs.
	ruff check $(sources)
	ruff format --check $(sources)

typecheck:  ## ERRORS ONLY: mypy over the gated library core.
	mypy src/rebar/reducer src/rebar/graph src/rebar/_store

check: lint typecheck  ## Run every check-only gate (no mutation).

test:  ## Run the default test suite (excludes integration + external).
	pytest -m "not integration and not external" -q
