PYTHON ?= .venv/bin/python

.PHONY: backend-install backend-test backend-lint backend-smoke ios-test ios-build ci-verify

backend-install:
	python3 -m venv .venv
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e '.[dev]'

backend-test:
	$(PYTHON) -m pytest

backend-lint:
	$(PYTHON) -m ruff check backend

backend-smoke:
	$(PYTHON) -m pytest backend/tests/test_smoke.py

ios-test:
	cd ios/TeVartaCore && swift test

ios-build:
	xcodebuild -project ios/TeVarta.xcodeproj -scheme TeVarta -sdk iphonesimulator -configuration Debug build

ci-verify:
	./scripts/verify_required_checks.sh
