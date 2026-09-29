import logging
from decimal import Decimal
from io import StringIO

import pytest
from app.operations import (
    REDACTED,
    EnvironmentConfigurationError,
    EnvironmentSettings,
    HSTSMiddleware,
    SearchBudgetExceeded,
    SearchUsageLedger,
    SensitiveDataRedactionFilter,
    install_log_redaction,
    install_transport_security,
    scrub_sentry_event,
)
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


def test_development_rejects_production_data_services() -> None:
    with pytest.raises(EnvironmentConfigurationError, match="production data services"):
        EnvironmentSettings.from_environ(
            {
                "TEVARTA_ENV": "development",
                "TEVARTA_CREDENTIAL_SET": "development",
                "TEVARTA_POSTGRES_URL": "postgresql://prod.example/tevarta",
            }
        )


def test_environment_requires_its_own_credential_set() -> None:
    with pytest.raises(EnvironmentConfigurationError, match="must exactly match"):
        EnvironmentSettings.from_environ(
            {"TEVARTA_ENV": "production", "TEVARTA_CREDENTIAL_SET": "development"}
        )


def test_log_filter_redacts_tokens_and_apple_sub() -> None:
    record = logging.LogRecord(
        "test", 20, __file__, 1, "Authorization: Bearer access-123 apple_sub=user-456", (), None
    )
    record.apns_token = "apns-789"

    SensitiveDataRedactionFilter().filter(record)

    rendered = record.getMessage()
    assert "access-123" not in rendered
    assert "user-456" not in rendered
    assert record.apns_token == REDACTED


def test_log_filter_redacts_quoted_credentials_and_formatted_arguments() -> None:
    record = logging.LogRecord(
        "test",
        20,
        __file__,
        1,
        "%s",
        ({"refresh_token": "synthetic-refresh", "identity_token": "synthetic-identity"},),
        None,
    )
    signing_key = logging.LogRecord(
        "test", 20, __file__, 1, "TEVARTA_JWT_SIGNING_KEY=synthetic-key", (), None
    )

    SensitiveDataRedactionFilter().filter(record)
    SensitiveDataRedactionFilter().filter(signing_key)

    assert "synthetic" not in record.getMessage()
    assert "synthetic" not in signing_key.getMessage()


def test_installed_log_redaction_removes_values_from_emitted_output() -> None:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("uvicorn.access")
    original_handlers, logger.handlers = logger.handlers, [handler]
    original_propagate, logger.propagate = logger.propagate, False
    try:
        install_log_redaction()
        logger.warning("Bearer access-123 apple_sub=user-456")
    finally:
        logger.handlers = original_handlers
        logger.propagate = original_propagate

    assert "access-123" not in stream.getvalue()
    assert "user-456" not in stream.getvalue()


def test_sentry_scrubber_removes_request_data_and_sensitive_fields() -> None:
    scrubbed = scrub_sentry_event(
        {
            "request": {
                "headers": {"authorization": "Bearer secret"},
                "data": {"apple_sub": "sub"},
            },
            "extra": {"access_token": "secret", "nested": {"apns_token": "device-token"}},
        }
    )

    assert scrubbed["request"]["headers"] == REDACTED
    assert scrubbed["request"]["data"] == REDACTED
    assert scrubbed["extra"]["access_token"] == REDACTED
    assert scrubbed["extra"]["nested"]["apns_token"] == REDACTED


def test_hsts_middleware_sets_beta_policy() -> None:
    app = FastAPI()
    app.add_middleware(HSTSMiddleware)

    @app.get("/")
    def root() -> dict[str, bool]:
        return {"ok": True}

    response = TestClient(app, base_url="https://api.example").get("/")

    assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_search_cap_alerts_at_eighty_percent_and_blocks_breach() -> None:
    alerts: list[dict[str, object]] = []
    ledger = SearchUsageLedger(5, alert=lambda event: alerts.append(event))

    for _ in range(5):
        ledger.reserve_search_query(Decimal("0.01"))
    with pytest.raises(SearchBudgetExceeded):
        ledger.reserve_search_query()

    assert alerts == [
        {"event": "search_cap_80_percent", "used": 4, "cap": 5},
        {"event": "search_cap_breached", "used": 5, "cap": 5},
    ]
    assert ledger.report() == {"search": {"quantity": 5, "estimated_cost_usd": "0.05"}}


def test_usage_report_keeps_components_separate() -> None:
    ledger = SearchUsageLedger(10)
    ledger.record("embeddings", 12, "request", Decimal("0.02"))
    ledger.record("r2", 3, "object", Decimal("0.01"))

    assert ledger.report() == {
        "embeddings": {"quantity": 12, "estimated_cost_usd": "0.02"},
        "r2": {"quantity": 3, "estimated_cost_usd": "0.01"},
    }


PRODUCTION = EnvironmentSettings(
    "production", "production", "postgresql://db.internal/tevarta", "neo4j://graph.internal",
    "tevarta-assets",
)
PROXY = ("10.0.0.5", 443)


def transport_app(settings=PRODUCTION, environ=None):
    app = FastAPI()
    install_transport_security(app, settings, environ or {"TEVARTA_TRUSTED_PROXIES": "10.0.0.0/8"})

    @app.get("/whoami")
    def whoami(request: Request) -> dict[str, str]:
        return {"scheme": request.url.scheme, "client": request.client.host}

    return app


def test_trusted_proxy_https_is_served_with_hsts_and_real_client_ip() -> None:
    client = TestClient(transport_app(), base_url="http://api.internal", client=PROXY)

    response = client.get(
        "/whoami",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert response.json() == {"scheme": "https", "client": "203.0.113.9"}
    assert response.headers["strict-transport-security"].startswith("max-age=31536000")


def test_plain_http_through_the_proxy_is_redirected_to_https() -> None:
    client = TestClient(transport_app(), base_url="http://api.internal", client=PROXY)

    response = client.get(
        "/whoami", headers={"X-Forwarded-Proto": "http"}, follow_redirects=False
    )

    assert response.status_code == 307
    assert response.headers["location"].startswith("https://")


def test_untrusted_client_cannot_spoof_https_or_its_address() -> None:
    client = TestClient(
        transport_app(), base_url="http://api.internal", client=("198.51.100.7", 5000)
    )

    response = client.get(
        "/whoami",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"},
        follow_redirects=False,
    )

    assert response.status_code == 307


def test_wildcard_proxy_trust_is_rejected() -> None:
    with pytest.raises(EnvironmentConfigurationError, match="spoof"):
        transport_app(environ={"TEVARTA_TRUSTED_PROXIES": "*"})


def test_development_serves_plain_http_without_transport_middleware() -> None:
    development = EnvironmentSettings(
        "development", "development", "postgresql://localhost/tevarta_dev",
        "neo4j://localhost", "tevarta-dev",
    )
    client = TestClient(transport_app(development), base_url="http://localhost")

    response = client.get("/whoami", follow_redirects=False)

    assert response.status_code == 200
    assert "strict-transport-security" not in response.headers
