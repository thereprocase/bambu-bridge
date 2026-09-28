.PHONY: test lint build addon-vendor integration-zip privacy
test:
	uv run pytest
lint:
	uv run ruff check src tests plugins companion
	uv run mypy src
build:
	uv build
addon-vendor:
	bash homeassistant/addon/sync-bridge-source.sh
integration-zip:
	bash homeassistant/integration/release-zip.sh
privacy:
	python3 scripts/privacy_check.py
