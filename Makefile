.DEFAULT_GOAL := help

.PHONY: help install dev test test-unit test-integration lint format type-check \
        docker-build docker-up docker-down clean setup-sqlcipher-macos

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-26s\033[0m %s\n", $$1, $$2}'

install: ## Install all Python dependencies (no SQLCipher required for unit tests)
	uv sync --extra dev

install-full: ## Install all deps including SQLCipher (run setup-sqlcipher-macos first)
	uv sync --all-extras

setup-sqlcipher-macos: ## Install SQLCipher on macOS via Homebrew (one-time)
	brew install sqlcipher
	LDFLAGS="-L$$(brew --prefix sqlcipher)/lib" \
	CFLAGS="-I$$(brew --prefix sqlcipher)/include/sqlcipher" \
	uv sync --all-extras

dev: ## Run the development server with auto-reload
	GITDR_DB_PASSPHRASE=$${GITDR_DB_PASSPHRASE:-devpassphrase} \
	uv run uvicorn gitdr.main:app --reload --port 8420

test: ## Run the full test suite with coverage (80% minimum)
	GITDR_DB_PASSPHRASE=testpassphrase \
	uv run pytest tests/ -v \
	  --cov=gitdr \
	  --cov-report=term-missing \
	  --cov-fail-under=80

test-unit: ## Run unit tests only
	GITDR_DB_PASSPHRASE=testpassphrase \
	uv run pytest tests/unit/ -v

test-integration: ## Run integration tests only
	GITDR_DB_PASSPHRASE=testpassphrase \
	uv run pytest tests/integration/ -v

lint: ## Lint with ruff
	uv run ruff check gitdr/ tests/

format: ## Format code with ruff
	uv run ruff format gitdr/ tests/

type-check: ## Run mypy static type checker
	uv run mypy gitdr/

docker-build: ## Build the Docker image
	docker compose build

docker-up: ## Start the stack in the background
	docker compose up -d

docker-down: ## Stop the stack
	docker compose down

clean: ## Remove build artefacts and caches
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -name "*.pyc" -delete
	rm -rf .pytest_cache .mypy_cache dist build *.egg-info .coverage htmlcov/ .ruff_cache/
