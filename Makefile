# Bambu Bridge â€” top-level build automation.
#
# All release artefacts land in dist/.
# The add-on vendor copy lives in homeassistant/addon/bambu-bridge/bridge/
# (included in reviewed release snapshots; produced by 'make addon-vendor').
#
# Quick reference:
#   make              â€” show this help
#   make build        â€” build wheel + sdist
#   make verify-dist  â€” sanity-check the built artefacts
#   make test         â€” run the test suite
#   make lint         â€” ruff + mypy
#   make clean        â€” remove dist/ and build/

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PYTHON   ?= python3
UV       ?= uv
RUFF     ?= $(UV) run ruff
MYPY     ?= $(UV) run mypy
PYTEST   ?= $(UV) run pytest

EXPORT_SCRIPT    := scripts/export-release.sh
INTEG_ZIP_SCRIPT := homeassistant/integration/release-zip.sh
ADDON_SYNC_SCRIPT := homeassistant/addon/sync-bridge-source.sh

# Four version sites that must stay in sync (see RELEASING.md).
VERSION_FILES := \
    pyproject.toml \
    src/bambu_bridge/__init__.py \
    homeassistant/addon/bambu-bridge/config.yaml \
    homeassistant/integration/custom_components/bambu_bridge/manifest.json

# ---------------------------------------------------------------------------
.PHONY: help build verify-dist integration-zip addon-vendor addon-vendor-check \
        bump-version export-release test lint clean

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) \
	    | awk -F ':.*## ' '{printf "  %-22s %s\n", $$1, $$2}' \
	    | sort

# ---------------------------------------------------------------------------
# Artefact builds
# ---------------------------------------------------------------------------
build: ## Build wheel + sdist into dist/
	$(UV) build

verify-dist: ## Verify dist/ artefacts are clean of secrets and include required files
	@set -e; \
	sdist="$$(ls dist/bambu_bridge-*.tar.gz 2>/dev/null | sort -V | tail -1)"; \
	wheel="$$(ls dist/bambu_bridge-*.whl   2>/dev/null | sort -V | tail -1)"; \
	if [ -z "$$sdist" ]; then echo "ERROR: no sdist found in dist/ â€” run 'make build' first" >&2; exit 1; fi; \
	if [ -z "$$wheel" ]; then echo "ERROR: no wheel found in dist/ â€” run 'make build' first" >&2; exit 1; fi; \
	echo "Checking sdist: $$sdist"; \
	tmpdir="$$(mktemp -d)"; \
	trap 'rm -rf "$$tmpdir"' EXIT; \
	tar -xzf "$$sdist" -C "$$tmpdir"; \
	fname_hits="$$(find "$$tmpdir" \( \
	    -iname 'SILVER-ACCESS*' -o -iname 'CONNECTING*' -o -iname 'recommend-*' \
	    -o -iname 'SYNTHESIS-*' -o -iname 'REPORT.md' -o -iname 'FOLLOWUP*' \
	    -o -path '*/dev-notes/*' -o -iname '*.jsonl' \
	    -o \( -iname '*.pem' ! -iname 'p1s-ca.pem.example' \) \
	\) 2>/dev/null || true)"; \
	if [ -n "$$fname_hits" ]; then \
	    echo "ABORT: secret/internal file found in sdist:" >&2; \
	    echo "$$fname_hits" >&2; \
	    exit 1; \
	fi; \
	content_hits="$$(grep -r --include='*.py' --include='*.md' --include='*.toml' \
	         --include='*.yaml' --include='*.yml' --include='*.sh' --include='*.json' \
	         -l -E 'PRIVATE_DEPLOYMENT_SENTINEL' \
	         "$$tmpdir" 2>/dev/null || true)"; \
	if [ -n "$$content_hits" ]; then \
	    echo "ABORT: leaked infra token found in sdist:" >&2; \
	    echo "$$content_hits" >&2; \
	    exit 1; \
	fi; \
	rm -rf "$$tmpdir"; trap - EXIT; \
	echo "  secret scan: clean"; \
	echo "Checking wheel: $$wheel"; \
	unzip -l "$$wheel" | grep -q "schema.sql"   || { echo "ERROR: schema.sql missing from wheel" >&2; exit 1; }; \
	unzip -l "$$wheel" | grep -q "viewer.html"  || { echo "ERROR: viewer.html missing from wheel" >&2; exit 1; }; \
	unzip -l "$$wheel" | grep -q "static/app"   || { echo "ERROR: static/app missing from wheel" >&2; exit 1; }; \
	unzip -l "$$wheel" | grep -q "LICENSE"       || { echo "ERROR: LICENSE missing from wheel" >&2; exit 1; }; \
	echo "  required files: present"; \
	echo "verify-dist: PASSED"

integration-zip: ## Package the HA integration into dist/bambu_bridge_integration-<ver>.zip
	bash $(INTEG_ZIP_SCRIPT)

addon-vendor: ## Vendor bridge source into the HA add-on build context
	bash $(ADDON_SYNC_SCRIPT)

addon-vendor-check: ## Check whether the vendored add-on source is up to date (exits non-zero if stale)
	bash $(ADDON_SYNC_SCRIPT) --check

# ---------------------------------------------------------------------------
# Version bump
# ---------------------------------------------------------------------------
bump-version: ## Bump version to VERSION=x.y.z in all 4 version sites
	@if [ -z "$(VERSION)" ]; then \
	    echo "Usage: make bump-version VERSION=x.y.z" >&2; \
	    exit 1; \
	fi
	@echo "Bumping version to $(VERSION) in:"
	@# pyproject.toml â€” two places: [project] version = and any occurrence
	sed -i 's/^version = "[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*"/version = "$(VERSION)"/' pyproject.toml
	@echo "  pyproject.toml"
	@# __init__.py
	sed -i 's/__version__ = "[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*"/__version__ = "$(VERSION)"/' \
	    src/bambu_bridge/__init__.py
	@echo "  src/bambu_bridge/__init__.py"
	@# HA add-on config.yaml
	sed -i 's/^version: "[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*"/version: "$(VERSION)"/' \
	    homeassistant/addon/bambu-bridge/config.yaml
	@echo "  homeassistant/addon/bambu-bridge/config.yaml"
	@# HA integration manifest.json
	sed -i 's/"version": "[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*"/"version": "$(VERSION)"/' \
	    homeassistant/integration/custom_components/bambu_bridge/manifest.json
	@echo "  homeassistant/integration/custom_components/bambu_bridge/manifest.json"
	@echo "Done. Update CHANGELOG.md and commit."

# ---------------------------------------------------------------------------
# Release export
# ---------------------------------------------------------------------------
export-release: ## Export a clean release tree via git archive (see RELEASING.md)
	bash $(EXPORT_SCRIPT)

# ---------------------------------------------------------------------------
# Dev
# ---------------------------------------------------------------------------
test: ## Run the test suite
	$(PYTEST) -q

lint: ## Run ruff + mypy
	$(RUFF) check src tests
	$(MYPY) src

clean: ## Remove dist/ and build/ artefacts
	rm -rf dist/ build/ src/*.egg-info src/bambu_bridge.egg-info
