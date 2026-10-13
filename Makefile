PYTHON ?= .venv/bin/python

.PHONY: backend-install backend-test backend-lint backend-smoke ios-test ios-build ci-verify

backend-install:
	python3 -m venv .venv
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e '.[dev]'

backend-test:
	$(PYTHON) -m pytest

backend-lint:
	$(PYTHON) -m ruff check backend migrations

.PHONY: database-test database-upgrade
database-test:
	@test -n "$$TEVARTA_TEST_POSTGRES_URL" || (echo "Set TEVARTA_TEST_POSTGRES_URL"; exit 1)
	$(PYTHON) -m pytest backend/tests/test_database.py

.PHONY: graph-test
# With a URI set, Neo4j fixtures connect or error; they never skip. Pytest exits
# non-zero if no neo4j-marked test is collected.
graph-test:
	@test -n "$$TEVARTA_TEST_NEO4J_URI" || (echo "Set TEVARTA_TEST_NEO4J_URI"; exit 1)
	$(PYTHON) -m pytest -m neo4j -rs

database-upgrade:
	$(PYTHON) -m alembic upgrade head

backend-smoke:
	$(PYTHON) -m pytest backend/tests/test_smoke.py

ios-test:
	cd ios/TeVartaCore && swift test

ios-build:
	xcodebuild -project ios/TeVarta.xcodeproj -scheme TeVarta -sdk iphonesimulator -configuration Debug build

ci-verify:
	./scripts/verify_required_checks.sh
