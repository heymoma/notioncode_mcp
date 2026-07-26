# Developer and operator entry points. `make help` lists everything.
SHELL := /bin/bash
ROOT := $(shell pwd)
VENV := $(ROOT)/.runtime/notion-agent-cli-venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
BRIDGE_PORT ?= 8765
RUNTIME_PORT ?= 8787
UNITS := notioncode-runtime.service notioncode-bridge.service

.DEFAULT_GOAL := help
.PHONY: help venv deps test test-python test-node lint check install \
        run-bridge run-runtime health metrics logs restart status \
        docker-build docker-up docker-down clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: ## Create the Python virtual environment
	@test -x $(PYTHON) || python3 -m venv $(VENV)

deps: venv ## Install Python and Node dependencies
	$(PIP) install --quiet -r requirements.txt
	$(PIP) install --quiet -e .
	npm --prefix services/mcp-runtime ci
	npm --prefix services/notion-private-mcp ci

test-python: ## Run the bridge test suite
	$(PYTHON) -m unittest discover -s tests/bridge -t . -v

test-node: ## Run the Node test suites
	npm --prefix services/mcp-runtime test
	node --test tests/node/*.test.mjs

test: test-python test-node ## Run every test suite

lint: ## Lint Python and syntax-check JavaScript
	$(VENV)/bin/ruff check src tests || ruff check src tests
	npm --prefix services/mcp-runtime run check
	npm --prefix services/notion-private-mcp run check
	bash -n scripts/install/linux.sh deploy/docker/entrypoint.sh scripts/dev/*.sh

check: lint test ## Everything CI runs
	node scripts/checks/check-layout.mjs
	node scripts/checks/check-public-release.mjs
	$(PYTHON) -m notion_bridge --check

install: ## Install the systemd services (needs root)
	sudo -H ./scripts/install/linux.sh

run-bridge: ## Run the bridge in the foreground
	./scripts/dev/run-bridge.sh

run-runtime: ## Run the coding-tools MCP runtime in the foreground
	./scripts/dev/run-runtime.sh

health: ## Print /healthz
	curl -fsS http://127.0.0.1:$(BRIDGE_PORT)/healthz | python3 -m json.tool

status: ## systemd status for both services
	systemctl --no-pager status $(UNITS) || true

restart: ## Restart both services
	sudo systemctl restart notioncode.target

logs: ## Follow bridge logs
	journalctl -fu notioncode-bridge.service -o cat

metrics: ## Print Prometheus metrics
	curl -fsS http://127.0.0.1:$(BRIDGE_PORT)/metrics

docker-build: ## Build the headless container image
	docker compose -f deploy/docker/docker-compose.yml build

docker-up: ## Start the headless container
	docker compose -f deploy/docker/docker-compose.yml up -d

docker-down: ## Stop the headless container
	docker compose -f deploy/docker/docker-compose.yml down

clean: ## Remove build and test artefacts (keeps credentials and state)
	find . -name '__pycache__' -type d -not -path './.runtime/*' -prune -exec rm -rf {} +
	rm -rf .ruff_cache src/*.egg-info
