.DEFAULT_GOAL := help
SHELL := /bin/sh
PYTHON ?= python3
NPM_REGISTRY ?= https://registry.npmjs.org/
PYPI_INDEX ?= https://pypi.org/simple/
TEST_ARGS ?=
BUMP ?= patch
VERSION ?=
export BUMP VERSION
-include local.mk
export npm_config_registry := $(NPM_REGISTRY)
export PYTHONWARNINGS := error
export PYTHONDONTWRITEBYTECODE := 1
PY := $(CURDIR)/.venv/bin/python
ifeq ($(OS),Windows_NT)
PY := $(CURDIR)/.venv/Scripts/python.exe
endif

.PHONY: help deps lock build build-typescript build-python lint format format-check test test-typescript test-python check pack publish publish-npm publish-pypi clean
.PHONY: version-bump github-release github-release-dry-run test-release

help: ## List repository targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-z0-9-]+:.*## / {printf "  %-22s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

deps: ## Install locked Node tools and a private Python development environment
	npm ci --registry="$(NPM_REGISTRY)"
	$(PYTHON) -m venv "$(CURDIR)/.venv"
	"$(PY)" -m pip install --index-url "$(PYPI_INDEX)" -e "./python[dev]"

lock: ## Refresh the Node dependency lockfile after manifest changes
	npm install --package-lock-only --ignore-scripts --registry="$(NPM_REGISTRY)"

build: build-typescript build-python ## Build both SDK distributions

build-typescript: ## Type-check and bundle the TypeScript SDK and declarations
	npm run build

build-python: ## Build the Python source distribution and wheel
	PYTHONDONTWRITEBYTECODE= "$(PY)" -m build --no-isolation python

lint: ## Check TypeScript/JavaScript and Python; warnings fail
	npm run lint
	"$(PY)" -m ruff check python scripts tests

format: ## Format repository documentation, Node sources, and Python sources
	npm run format
	"$(PY)" -m ruff format python scripts tests

format-check: ## Check formatting without modifying files
	npm run format:check
	"$(PY)" -m ruff format --check python scripts tests

test: test-typescript test-python test-release ## Run SDK and release-tooling test suites

test-typescript: build-typescript ## Run standalone Node tests and optional external-service tests
	npm test

test-python: ## Run standalone Python tests and optional external-service tests
	"$(PY)" -W error -m unittest discover -s python/tests -v $(TEST_ARGS)

test-release: ## Check version bumps and GitHub release preconditions without publishing
	"$(PY)" -W error -m unittest discover -s tests -v

check: lint format-check build test ## Run all repository checks

pack: build ## Prepare and validate publishable npm/PyPI artifacts without uploading
	mkdir -p dist
	npm pack --workspace @sloppy-plugins/sdk --pack-destination "$(CURDIR)/dist"
	"$(PY)" -m twine check python/dist/*

publish: publish-npm publish-pypi ## Publish both SDK packages to the public registries

version-bump: ## Bump both SDKs and lockfile (BUMP=patch|minor|major, or VERSION=X.Y.Z)
	"$(PYTHON)" scripts/release.py bump --part "$$BUMP" $${VERSION:+--version "$$VERSION"}

github-release: check pack ## Publish a GitHub Release at the current SDK version and commit
	"$(PYTHON)" scripts/release.py github

github-release-dry-run: check pack ## Check locally and preview the GitHub Release command
	"$(PYTHON)" scripts/release.py github --dry-run

publish-npm: check ## Publish the TypeScript package to public npm
	npm publish --workspace @sloppy-plugins/sdk --access public --registry=https://registry.npmjs.org/

publish-pypi: check ## Publish the Python package to public PyPI
	"$(PY)" -m twine check python/dist/*
	"$(PY)" -m twine upload --repository-url https://upload.pypi.org/legacy/ python/dist/*

clean: ## Remove generated artifacts and caches, preserving installed dependencies
	node scripts/clean.mjs
