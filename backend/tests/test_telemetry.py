from datetime import UTC, datetime

import pytest
from app import mock_api
from app.contracts import FeedbackRequest
from app.main import app
from app.telemetry import MemoryTelemetryStore, RateLimitExceeded, TelemetryService, UserRateLimiter
from fastapi.testclient import TestClient


def event(**changes: object) -> FeedbackRequest:
    payload = {
        "event_id": "evt_test",
        "action": "card_impression",
        "slot_type": "graph",
        "slot_position": 1,
        "occurred_at": "2026-09-13T00:00:00Z",
        "idempotency_key": "telemetry-key-1",
    } | changes
    return FeedbackRequest.model_validate(payload)


def test_retry_does_not_double_count_and_zero_action_impression_is_saved() -> None:
    service = TelemetryService(MemoryTelemetryStore())
    assert service.record("usr_a", event()).deduplicated is False
    assert service.record("usr_a", event()).deduplicated is True


def test_feedback_rate_limit_rejects_excess() -> None:
    now = datetime(2026, 9, 13, tzinfo=UTC)
    service = TelemetryService(MemoryTelemetryStore(), UserRateLimiter(limit=1), now=lambda: now)
    service.record("usr_a", event())
    with pytest.raises(RateLimitExceeded):
        service.record("usr_a", event(idempotency_key="telemetry-key-2"))


def test_card_context_and_session_duration_are_required() -> None:
    with pytest.raises(ValueError):
        event(slot_position=None)
    with pytest.raises(ValueError):
        event(action="session_length", event_id=None, slot_type=None, slot_position=None)


@pytest.mark.parametrize(
    "action",
    [
        "app_open",
        "feed_view",
        "card_impression",
        "card_click",
        "card_dismiss",
        "not_relevant",
        "save",
        "push_received",
        "push_opened",
        "digest_opened",
        "seed_added",
        "seed_removed",
        "session_length",
    ],
)
def test_every_required_telemetry_action_validates(action: str) -> None:
    payload: dict[str, object] = {
        "action": action,
        "occurred_at": "2026-09-13T00:00:00Z",
        "idempotency_key": f"telemetry-{action}",
    }
    if action in {"card_impression", "card_click", "card_dismiss", "not_relevant", "save"}:
        payload |= {"event_id": "evt_test", "slot_type": "graph", "slot_position": 1}
    if action == "session_length":
        payload["session_duration_s"] = 5

    assert FeedbackRequest.model_validate(payload).action == action


def test_legacy_action_aliases_are_normalized_and_duration_is_finite() -> None:
    assert event(action="impression").action == "card_impression"
    with pytest.raises(ValueError):
        event(action="session_length", event_id=None, session_duration_s=float("inf"))


def test_feedback_endpoint_requires_authorization() -> None:
    response = TestClient(app).post("/v1/feedback", json=event().model_dump(mode="json"))

    assert response.status_code == 401


def test_feedback_endpoint_deduplicates_a_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mock_api, "telemetry_service", TelemetryService(MemoryTelemetryStore()))
    client = TestClient(app)
    headers = {"Authorization": "Bearer fixture-access-token"}

    first = client.post("/v1/feedback", headers=headers, json=event().model_dump(mode="json"))
    second = client.post("/v1/feedback", headers=headers, json=event().model_dump(mode="json"))

    assert first.status_code == 202
    assert first.json() == {"accepted": True, "deduplicated": False}
    assert second.status_code == 202
    assert second.json() == {"accepted": True, "deduplicated": True}


def test_feedback_endpoint_rate_limits_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 9, 13, tzinfo=UTC)
    monkeypatch.setattr(
        mock_api,
        "telemetry_service",
        TelemetryService(MemoryTelemetryStore(), UserRateLimiter(limit=1), now=lambda: now),
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer fixture-access-token"}

    first_response = client.post(
        "/v1/feedback", headers=headers, json=event().model_dump(mode="json")
    )
    assert first_response.status_code == 202
    response = client.post(
        "/v1/feedback",
        headers=headers,
        json=event(idempotency_key="telemetry-key-2").model_dump(mode="json"),
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
