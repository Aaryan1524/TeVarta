"""Guard the production Compose file's security shape. Docker is not needed to run this."""

from pathlib import Path

import yaml

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
COMPOSE = yaml.safe_load((DEPLOY / "compose.production.yaml").read_text())
SERVICES = COMPOSE["services"]


def networks(service: str) -> set[str]:
    declared = SERVICES[service].get("networks", [])
    return set(declared if isinstance(declared, list) else declared)


def test_only_caddy_publishes_ports():
    assert [name for name, spec in SERVICES.items() if "ports" in spec] == ["caddy"]
    assert SERVICES["caddy"]["ports"] == ["80:80", "443:443"]


def test_data_stores_sit_on_the_internal_network_only():
    assert COMPOSE["networks"]["private"]["internal"] is True
    for store in ("postgres", "neo4j", "redis"):
        assert networks(store) == {"private"}, store


def test_only_the_worker_and_api_can_reach_the_internet():
    reaches_out = {name for name in SERVICES if networks(name) - {"private"}}
    assert reaches_out == {"api", "worker", "caddy"}


def test_credentials_come_from_secret_files_not_the_compose_file():
    text = (DEPLOY / "compose.production.yaml").read_text()
    assert "POSTGRES_PASSWORD:" not in text and "NEO4J_AUTH:" not in text
    assert SERVICES["postgres"]["environment"]["POSTGRES_PASSWORD_FILE"].startswith("/run/secrets/")
    assert SERVICES["neo4j"]["environment"]["NEO4J_AUTH_FILE"].startswith("/run/secrets/")


def test_the_api_trusts_exactly_the_proxy_address_the_compose_file_assigns():
    assigned = SERVICES["caddy"]["networks"]["edge"]["ipv4_address"]
    guide = (DEPLOY / "README.md").read_text()
    assert f"TEVARTA_TRUSTED_PROXIES={assigned}" in guide


def test_one_image_serves_every_app_service_and_is_built_once():
    app_services = ("migrate", "api", "worker", "beat")
    assert len({SERVICES[name]["image"] for name in app_services}) == 1
    assert [name for name in SERVICES if "build" in SERVICES[name]] == ["migrate"]


def test_the_api_waits_for_migrations_and_the_stores():
    depends = SERVICES["api"]["depends_on"]
    assert depends["migrate"]["condition"] == "service_completed_successfully"
    assert depends["neo4j"]["condition"] == "service_healthy"
