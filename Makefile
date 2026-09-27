SHELL := /usr/bin/bash
.DEFAULT_GOAL := help

REPO_ROOT := $(CURDIR)
PYTHON_VERSION := 3.13.14
NODE_VERSION := 24.18.0
NODE_HOME := $(REPO_ROOT)/.tools/node-v$(NODE_VERSION)-linux-x64
PNPM_HOME := $(REPO_ROOT)/.tools/pnpm
export UV_CACHE_DIR := $(REPO_ROOT)/.uv-cache
export UV_PYTHON_INSTALL_DIR := $(REPO_ROOT)/.tools/python
export PATH := $(NODE_HOME)/bin:$(PNPM_HOME)/bin:/usr/local/bin:/usr/bin:/bin
export TMPDIR := /tmp
export TMP := /tmp
export TEMP := /tmp
export PENSAE_RESTORE_BACKUP := $(BACKUP)
export PENSAE_RESTORE_CONFIRMATION := $(CONFIRM)

.PHONY: help bootstrap infra-up infra-down start stop status test test-integration test-browser check backup restore release-check release-freeze-locks release-static release-deterministic release-integration release-empty-migration release-generated-client release-frontend-build release-browser-scenarios release-packaging release-cleanup

help:
	@echo "Pensae Signal native Fedora 44 commands: bootstrap infra-up infra-down start stop status test test-integration test-browser check backup restore release-check"

bootstrap:
	@./scripts/linux/bootstrap.sh

infra-up:
	docker compose up --detach --wait

infra-down:
	uv run --frozen --offline --python $(PYTHON_VERSION) python -m scripts.linux.compose_down

.PHONY: configure-secrets install-secret-scanner secrets-check
configure-secrets:
	python3 scripts/linux/configure_secrets.py

install-secret-scanner:
	python3 -m scripts.release.publication install

secrets-check:
	python3 -m scripts.release.publication scan

start:
	uv run --python $(PYTHON_VERSION) python scripts/linux/launcher.py start

stop:
	uv run --python $(PYTHON_VERSION) python scripts/linux/launcher.py stop

status:
	uv run --python $(PYTHON_VERSION) python scripts/linux/launcher.py status

test:
	uv run --python $(PYTHON_VERSION) pytest -m "not integration"
	pnpm --dir frontend test --run

test-integration:
	@./scripts/linux/test_integration.sh

test-browser:
	@./scripts/linux/test_browser.sh

check:
	uv run --frozen --offline --python $(PYTHON_VERSION) ruff format --check src tests scripts migrations
	uv run --frozen --offline --python $(PYTHON_VERSION) ruff check src tests scripts migrations
	uv run --frozen --offline --python $(PYTHON_VERSION) pyright
	uv run --frozen --offline --python $(PYTHON_VERSION) python scripts/check_architecture.py
	uv run --frozen --offline --python $(PYTHON_VERSION) pytest -m "not integration"
	pnpm --dir frontend check
	pnpm --dir frontend test --run
	pnpm --dir frontend build
	@./scripts/check_generated_client.sh

backup:
	uv run --frozen --offline --python $(PYTHON_VERSION) python -m pensae.operations backup

restore:
	uv run --frozen --offline --python $(PYTHON_VERSION) python -m pensae.operations restore

release-check:
	@./scripts/linux/release_check.sh

release-freeze-locks:
	uv lock --check --offline
	pnpm install --frozen-lockfile --offline --ignore-scripts
	git diff --exit-code -- uv.lock pnpm-lock.yaml pyproject.toml package.json frontend/package.json

release-static:
	uv run --frozen --offline --python $(PYTHON_VERSION) ruff format --check src tests scripts migrations
	uv run --frozen --offline --python $(PYTHON_VERSION) ruff check src tests scripts migrations
	uv run --frozen --offline --python $(PYTHON_VERSION) pyright
	uv run --frozen --offline --python $(PYTHON_VERSION) python scripts/check_architecture.py
	pnpm --dir frontend check

release-deterministic:
	uv run --frozen --offline --python $(PYTHON_VERSION) pytest -m "not integration"
	pnpm --dir frontend test --run

release-integration:
	@./scripts/linux/test_integration.sh

release-empty-migration:
	@./scripts/linux/test_empty_migration.sh

release-generated-client:
	@./scripts/check_generated_client.sh

release-frontend-build:
	pnpm --dir frontend build

release-browser-scenarios:
	@PENSAE_SKIP_FRONTEND_BUILD=1 PENSAE_BROWSER_RELEASE_ONLY=1 ./scripts/linux/test_browser.sh

release-packaging:
	$(MAKE) secrets-check
	uv run --frozen --offline --python $(PYTHON_VERSION) python -m scripts.release.audit --scope packaging

release-cleanup:
	uv run --frozen --offline --python $(PYTHON_VERSION) python -m scripts.release.audit --scope cleanup
