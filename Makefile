.PHONY: help build up down restart logs logs-all logs-webhook logs-sdk ps \
	       prod-up prod-down prod-pull prod-start \
	       lint lint-fix lint-fast test test-unit \
	       test-integration clean clean-logs prune ngrok start

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

# --- Compose file selection ---
# Core only (default):   make start
# With Langfuse:          make start LANGFUSE=true
# This flag applies to ALL targets: build, up, down, logs, ps, etc.

ifeq ($(LANGFUSE),true)
COMPOSE = docker compose -f docker-compose.yml -f docker-compose.langfuse.yml
else
COMPOSE = docker compose
endif

# --- Docker (development) ---

build: ## Build all Docker images. Use LANGFUSE=true to include Langfuse images.
	@mkdir -p logs/langfuse
	$(COMPOSE) build

# Scale only what was asked for, so an unnamed worker keeps the replica
# count from docker-compose.yml instead of being reset to 1.
SCALE = $(if $(SANDBOX),--scale sandbox_worker=$(SANDBOX)) \
	$(if $(MEMORY),--scale memory_worker=$(MEMORY)) \
	$(if $(RETRO),--scale retrospector_worker=$(RETRO))

up: ## Start services (detached). Optional: SANDBOX=10 MEMORY=2 RETRO=2 LANGFUSE=true
	@mkdir -p logs/langfuse
	$(COMPOSE) up -d $(SCALE)

down: ## Stop services. Use LANGFUSE=true to also stop Langfuse services.
	$(COMPOSE) down

restart: down up ## Restart all services

logs: ## Tail logs for bot services only
	$(COMPOSE) logs -f webhook worker sandbox_worker memory_worker retrospector_worker repo_sync scheduler session-service

logs-all: ## Tail logs for all services
	$(COMPOSE) logs -f

logs-webhook: ## Tail webhook logs
	$(COMPOSE) logs -f webhook

logs-sdk: ## Tail sandbox worker logs
	$(COMPOSE) logs -f sandbox_worker

logs-session-service: ## Tail session service logs
	$(COMPOSE) logs -f session-service

ps: ## List running services
	$(COMPOSE) ps

# --- Docker (production — pre-built images) ---

PROD_COMPOSE = docker compose -f docker-compose.prod.yml
PROD_COMPOSE_LANGFUSE = docker compose -f docker-compose.prod.yml -f docker-compose.langfuse.yml

prod-pull: ## Pull latest pre-built images from GHCR
	$(PROD_COMPOSE) pull

prod-up: ## Start production services (pre-built images). Add LANGFUSE=true for observability.
	$(PROD_COMPOSE) up -d

prod-down: ## Stop production services
	$(PROD_COMPOSE) down

prod-start: prod-pull prod-up ## Pull and start production services

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

NGROK_OPTS ?=

ngrok: ## Start ngrok tunnel to webhook on port 10000
	ngrok http 10000 $(NGROK_OPTS)

# --- Dev workflows ---

start: build up ngrok ## Build, start services, and open ngrok tunnel. Add LANGFUSE=true for observability.

# --- Cleanup ---

clean: down ## Stop services and remove volumes
	$(COMPOSE) down -v

clean-logs: ## Clear all log files
	rm -f logs/*.log logs/langfuse/*.log

prune: ## Remove unused Docker resources
	docker system prune -f
