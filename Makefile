.DEFAULT_GOAL := help

# Print a free host port at or above $(PORT) (stdlib only; no lsof/nc needed). A script, not an
# inline `python -c`: make leaves the \n of a one-liner as two characters, which Python rejects.
PORT ?= 8080
FREE_PORT := python3 scripts/free_port.py $(PORT)

.PHONY: demo-video docs docs-serve run demo docker image-check image-smoke image-smoke-hosted droplet-smoke test-browser release-check cli-binary bench test-embeddings help setup hooks fmt lint typecheck test-fast test-postgres test-e2e test check audit secrets clean

help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

run: ## Run the server on the first free port from 8080; the dashboard is at /ui/ (Ctrl+C to stop)
	@P=$$($(FREE_PORT)); echo ">>> semula: http://127.0.0.1:$$P/ui/   (health: /health)"; uv run semula serve --port $$P

demo: ## Run with demo data and a moving log; prints the dashboard URL (ARGS=--record parks calls)
	@uv run python scripts/demo.py --port $(PORT) $(ARGS)

demo-video: ## Re-record docs/assets/semula-dashboard-demo.mp4 (needs ffmpeg, Chromium and the video-demo skill)
	uv run --extra dev --with playwright python scripts/video/record_dashboard_demo.py

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

image-check: ## Build the image and enforce its size, cold-start and /data budgets (needs Docker)
	scripts/check_image.sh semula:check

image-smoke: ## Start IMAGE (default semula:check) and check /health, /v1, a miss and /ui (needs Docker)
	scripts/smoke_image.sh $(or $(IMAGE),semula:check)

image-smoke-hosted: ## Check a hosted image (IMAGE, default semula:hosted-check, built with the hosted and postgres extras)
	scripts/smoke_image_hosted.sh $(or $(IMAGE),semula:hosted-check)

droplet-smoke: ## Bring up deploy/droplet (Caddy, app, Postgres or, with VARIANT=sqlite, SQLite) on a throwaway name and check it (IMAGE, default semula:hosted-check; needs Docker)
	scripts/smoke_droplet.sh $(or $(IMAGE),semula:hosted-check) $(or $(VARIANT),postgres)

PLAYWRIGHT ?= playwright==1.64.1
test-browser: ## Real browsers against the real dashboard: stored text never runs, it fits a phone, the keyboard works (an engine that cannot start here is skipped). First: uv run --with $(PLAYWRIGHT) playwright install chromium firefox webkit
	SEMULA_BROWSERS=$(or $(BROWSERS),chromium,firefox,webkit) uv run --extra dev --with $(PLAYWRIGHT) pytest -m browser -q

release-check: ## Would TAG (like v0.1.0) release? Tag, pyproject, lockfile and CHANGELOG must agree
	python3 scripts/release.py check $(TAG)

ZENSICAL ?= zensical==0.0.71
docs: ## Build the documentation site into site/ (Zensical; fails on a broken build)
	uv run --no-project --with $(ZENSICAL) zensical build --clean --strict

docs-serve: ## Serve the documentation with live reload on http://127.0.0.1:8000
	uv run --no-project --with $(ZENSICAL) zensical serve

cli-binary: ## Build the standalone CLI binary into dist/ (PyInstaller; HTTP commands only)
	scripts/build_cli_binary.sh dist

bench: ## Replay latency on a 1k-entry project; fails over the 15 ms p95 budget
	uv run pytest -m perf -s -q

test-embeddings: ## The real embedding model: paraphrase, offline load, latency (downloads 65 MB once)
	uv run --extra dev --extra embeddings pytest -m embeddings -s -q

test-fast: ## Fast layer: unit + integration, no infra
	uv run pytest -m "unit or integration" -q

PG_PORT ?= 55432

test-postgres: ## Run unit, integration and e2e tests against a throwaway Postgres (needs Docker)
	@docker rm -f semula-test-pg >/dev/null 2>&1; \
	docker run -d --rm --name semula-test-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=semula \
		-p 127.0.0.1:$(PG_PORT):5432 postgres:16-alpine >/dev/null && \
	trap 'docker stop semula-test-pg >/dev/null' EXIT; \
	for i in $$(seq 1 40); do docker exec semula-test-pg pg_isready -h 127.0.0.1 -U postgres -d semula >/dev/null 2>&1 && break; sleep 1; done; \
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
