.DEFAULT_GOAL := help

.PHONY: help setup hooks fmt lint typecheck test-fast test-e2e test check audit secrets clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Install dev dependencies into a local venv
	uv sync --extra dev

hooks: ## Install the git pre-push hook (bypass once with: git push --no-verify)
	git config core.hooksPath .githooks
	@echo "pre-push hook installed"

fmt: ## Auto-format the code
	uv run ruff format .
	uv run ruff check --fix .

lint: ## Lint and format-check (no changes)
	uv run ruff check .
	uv run ruff format --check .

typecheck: ## Type-check with mypy (strict)
	uv run mypy

test-fast: ## Fast layer: unit + integration, no infra
	uv run pytest -m "unit or integration" -q

test-e2e: ## Heavy layer: real container (needs Docker)
	uv run pytest -m e2e -q

test: ## Full test suite
	uv run pytest -q

check: lint typecheck test-fast ## The pre-push gate: lint, types, fast tests

audit: ## Scan dependencies for known vulnerabilities
	uv run pip-audit

secrets: ## Scan the working tree and history for committed secrets
	gitleaks detect --no-banner

clean: ## Remove build and cache artifacts
	rm -rf dist build .pytest_cache .ruff_cache .mypy_cache
