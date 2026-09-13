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
    scrub_sentry_event,
)
from fastapi import FastAPI
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
