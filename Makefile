# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

PROJECT_ROOT := $(CURDIR)

# Shell strict mode
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

CHARMCRAFT_YAML := $(PROJECT_ROOT)/charmcraft.yaml
IMPORT_SCRIPT := $(PROJECT_ROOT)/scripts/import_rock.sh

REGISTRY := localhost:32000

# Ensure yq is installed: 'sudo snap install yq'
CHARM_NAME := $(shell yq '.name' $(CHARMCRAFT_YAML))
CHARM_BASE := $(shell yq '.bases[0].run-on[0].name' $(CHARMCRAFT_YAML))-$(shell yq '.bases[0].run-on[0].channel' $(CHARMCRAFT_YAML))
CHARM_ARCH := amd64
CHARM_FILE := $(PROJECT_ROOT)/$(CHARM_NAME)_$(CHARM_BASE)-$(CHARM_ARCH).charm

ROCK_DIR := $(PROJECT_ROOT)/rock
ROCKCRAFT_YAML := $(ROCK_DIR)/rockcraft.yaml
ROCK_NAME := $(shell yq '.name' $(ROCKCRAFT_YAML))
ROCK_VERSION := $(shell yq '.version' $(ROCKCRAFT_YAML))
ROCK := $(ROCK_DIR)/$(ROCK_NAME)_$(ROCK_VERSION)_$(CHARM_ARCH).rock

.PHONY: all
all: build

.PHONY: help
help:
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@echo "  build             Build the charm and the rock"
	@echo "  build-charm       Build the charm using charmcraft"
	@echo "  build-rock        Build the rock using rockcraft"
	@echo "  check-build-deps  Check if build dependencies are installed"
	@echo "  check-deploy-deps Check if deployment dependencies are installed"
	@echo "  check-deps        Check if all dependencies are installed"
	@echo "  checks            Run fmt, lint, unit and static tests"
	@echo "  clean             Remove built charm and rock files"
	@echo "  clean-dev         Remove tox/pytest/mypy caches, coverage data and __pycache__"
	@echo "  deploy-local      Deploy the built charm with the locally imported rock"
	@echo "  fmt               Apply coding style standards to the code"
	@echo "  import-rock       Build and import the rock into the MicroK8s registry"
	@echo "  lint              Check the code against coding style standards"
	@echo "  test              Run unit, rock entrypoint and static tests"
	@echo "  test-integration  Run integration tests"
	@echo "  test-serve        Run the rock entrypoint tests"
	@echo "  test-static       Run static analysis tests"
	@echo "  test-unit         Run unit tests"
	@echo "  help              Show this help message"
	@echo "  venv              Create a virtual environment"

.PHONY: build
build: build-charm build-rock

# --- Dependency checks ---

.PHONY: check-build-deps
check-build-deps:
	@which yq >/dev/null || (echo "yq not found" && exit 1)
	@which charmcraft >/dev/null || (echo "charmcraft not found" && exit 1)
	@which rockcraft >/dev/null || (echo "rockcraft not found" && exit 1)
	@which tox >/dev/null || (echo "tox not found" && exit 1)
	@echo "All build dependencies are installed."

.PHONY: check-deploy-deps
check-deploy-deps:
	@which juju >/dev/null || (echo "juju not found" && exit 1)
	@which docker >/dev/null || (echo "docker not found" && exit 1)
	@which microk8s >/dev/null || (echo "microk8s not found" && exit 1)
	@which skopeo >/dev/null || (echo "skopeo not found" && exit 1)
	@echo "All deployment dependencies are installed."

.PHONY: check-deps
check-deps: check-build-deps check-deploy-deps

# --- Code quality ---

.PHONY: checks
checks: fmt lint test

.PHONY: fmt
fmt:
	tox -e fmt

.PHONY: lint
lint:
	tox -e lint

# --- Tests ---

.PHONY: test
test: test-unit test-serve test-static

.PHONY: test-integration
test-integration:
	tox -e integration

.PHONY: test-serve
test-serve:
	tox -e serve

.PHONY: test-static
test-static:
	tox -e static

.PHONY: test-unit
test-unit:
	tox -e unit

# --- Build ---

.PHONY: build-charm
build-charm:
	@echo "Building charm..."
	cd $(PROJECT_ROOT) && charmcraft pack --use-lxd --verbose

$(ROCK): $(ROCKCRAFT_YAML) $(ROCK_DIR)/files/serve.py
	@echo "Building rock..."
	cd $(ROCK_DIR) && rockcraft pack --use-lxd --verbose

.PHONY: build-rock
build-rock: $(ROCK)

.PHONY: import-rock
import-rock: $(ROCK)
	@echo "Importing rock $(ROCK)..."
	$(IMPORT_SCRIPT) $(ROCK) $(ROCK_NAME) $(ROCK_VERSION) --latest

# --- Deploy ---

.PHONY: deploy-local
deploy-local:
	@DIGEST=$$(skopeo inspect --tls-verify=false docker://$(REGISTRY)/$(ROCK_NAME):latest 2>/dev/null | jq -r '.Digest' || echo "latest") && \
	echo "Deploying charm with the local rock..." && \
	juju deploy $(CHARM_FILE) \
		--resource $(ROCK_NAME)=$(REGISTRY)/$(ROCK_NAME)@$$DIGEST

# --- Clean ---

.PHONY: clean
clean:
	@echo "Cleaning up..."
	rm -f $(PROJECT_ROOT)/*.charm
	rm -f $(ROCK_DIR)/*.rock

.PHONY: clean-dev
clean-dev:
	@echo "Cleaning developer caches and tool artifacts..."
	rm -rf $(PROJECT_ROOT)/.tox
	rm -rf $(PROJECT_ROOT)/.mypy_cache
	rm -rf $(PROJECT_ROOT)/.pytest_cache
	rm -rf $(PROJECT_ROOT)/.ruff_cache
	rm -rf $(PROJECT_ROOT)/htmlcov
	rm -rf $(PROJECT_ROOT)/build
	rm -rf $(PROJECT_ROOT)/dist
	rm -f  $(PROJECT_ROOT)/.coverage
	rm -f  $(PROJECT_ROOT)/coverage.xml
	find $(PROJECT_ROOT) -path '*/.git' -prune -o -type d -name '__pycache__' -exec rm -rf {} +
	find $(PROJECT_ROOT) -path '*/.git' -prune -o -type d -name '*.egg-info' -exec rm -rf {} +

# --- Virtual environment ---

.PHONY: venv
venv:
	tox devenv -e integration
