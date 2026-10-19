.DEFAULT_GOAL := help

# Print a free host port at or above $(PORT) (stdlib only; no lsof/nc needed).
PORT ?= 8080
FREE_PORT := python3 -c 'import socket,sys\np=int(sys.argv[1])\nwhile True:\n    s=socket.socket()\n    try:\n        s.bind(("127.0.0.1",p)); s.close(); print(p); break\n    except OSError:\n        s.close(); p+=1' $(PORT)

.PHONY: run docker help setup hooks fmt lint typecheck test-fast test-postgres test-e2e test check audit secrets clean

help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

run: ## Run the server on the first free port from 8080 (Ctrl+C to stop)
	@P=$$($(FREE_PORT)); echo ">>> semula: http://127.0.0.1:$$P/health"; uv run semula serve --port $$P

docker: ## Build and run the container on the first free port from 8080
	@P=$$($(FREE_PORT)); echo ">>> semula: http://127.0.0.1:$$P/health"; docker build -q -t semula:dev . && docker run --rm -p 127.0.0.1:$$P:8080 -v semula-data:/data semula:dev

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

PG_PORT ?= 55432

test-postgres: ## Run unit, integration and e2e tests against a throwaway Postgres (needs Docker)
	@docker rm -f semula-test-pg >/dev/null 2>&1; \
	docker run -d --rm --name semula-test-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=semula \
		-p 127.0.0.1:$(PG_PORT):5432 postgres:16-alpine >/dev/null && \
	trap 'docker stop semula-test-pg >/dev/null' EXIT; \
	for i in $$(seq 1 40); do docker exec semula-test-pg pg_isready -U postgres -d semula >/dev/null 2>&1 && break; sleep 1; done; \
	SEMULA_TEST_DATABASE_URL=postgresql://postgres:pw@127.0.0.1:$(PG_PORT)/semula \
		uv run --extra dev --extra e2e pytest -m "unit or integration or e2e" -q

test-e2e: ## Heavy layer: real server driven by OpenAI SDK and LangChain
	uv run --extra dev --extra e2e pytest -m e2e -q

test: ## Full test suite (unit, integration, e2e)
	uv run --extra dev --extra e2e pytest -q

check: lint typecheck test-fast ## The pre-push gate: lint, types, fast tests

audit: ## Scan dependencies for known vulnerabilities
	uv run pip-audit

secrets: ## Scan the working tree and history for committed secrets
	gitleaks detect --no-banner

clean: ## Remove build and cache artifacts
	rm -rf dist build .pytest_cache .ruff_cache .mypy_cache
