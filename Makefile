.PHONY: help build up down restart logs logs-all lint lint-fix test clean ngrok start

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

# --- Docker ---

build: ## Build all Docker images
	@mkdir -p logs/langfuse
	docker compose build

up: ## Start all services (detached)
	@mkdir -p logs/langfuse
	docker compose up -d

down: ## Stop all services
	docker compose down

restart: down up ## Restart all services

logs: ## Tail logs for bot services only
	docker compose logs -f webhook worker sandbox_worker memory_worker retrospector_worker repo_sync indexing_worker

logs-all: ## Tail logs for all services
	docker compose logs -f

logs-webhook: ## Tail webhook logs
	docker compose logs -f webhook

logs-sdk: ## Tail sandbox worker logs
	docker compose logs -f sandbox_worker

ps: ## List running services
	docker compose ps

# --- Minimal stack (no observability) ---

up-minimal: ## Start minimal stack (Redis + webhook + worker)
	docker compose -f docker-compose.minimal.yml up -d

down-minimal: ## Stop minimal stack
	docker compose -f docker-compose.minimal.yml down

# --- Code Quality ---

lint: ## Run all code quality checks
	bash ./check-code.sh

lint-fix: ## Auto-fix formatting and lint issues
	bash ./check-code.sh --fix

lint-fast: ## Run checks (skip mypy)
	bash ./check-code.sh --fast

# --- Testing ---

test: ## Run tests
	python -m pytest tests/ -v

test-unit: ## Run unit tests only
	python -m pytest tests/ -v -m unit

test-integration: ## Run integration tests only
	python -m pytest tests/ -v -m integration

# --- Ngrok ---

ngrok: ## Start ngrok tunnel to webhook on port 10000
	ngrok http 10000

# --- Dev workflows ---

start: build up ngrok ## Build, start services, and open ngrok tunnel

# --- Cleanup ---

clean: down ## Stop services and remove volumes
	docker compose down -v

clean-logs: ## Clear all log files
	rm -f logs/*.log logs/langfuse/*.log

prune: ## Remove unused Docker resources
	docker system prune -f
