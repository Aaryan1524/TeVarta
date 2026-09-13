import json
from pathlib import Path

import pytest
from app.contracts import (
    AccountDeletionResponse,
    AuthResponse,
    DeviceResponse,
    EntityResponse,
    EntitySearchResponse,
    ErrorEnvelope,
    EventDetailResponse,
    FeedbackResponse,
    FeedResponse,
    GraphResponse,
    LoadingResponse,
    ProfileResponse,
    SeedResponse,
    SeedsStatusResponse,
)
from app.main import app
from app.mock_api import router as mock_router
from fastapi.testclient import TestClient

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "v1"
AUTH_HEADERS = {"Authorization": "Bearer fixture-access-token"}


@pytest.mark.parametrize(
    ("file_name", "model"),
    [
        ("auth_success.json", AuthResponse),
        ("entity_search_ambiguous.json", EntitySearchResponse),
        ("entity_search_success.json", EntitySearchResponse),
        ("seed_success.json", SeedResponse),
        ("seeds_status.json", SeedsStatusResponse),
        ("feed_success.json", FeedResponse),
        ("feed_empty.json", FeedResponse),
        ("loading.json", LoadingResponse),
        ("event_success.json", EventDetailResponse),
        ("event_merged.json", EventDetailResponse),
        ("graph.json", GraphResponse),
        ("entity_success.json", EntityResponse),
        ("profile.json", ProfileResponse),
        ("device_success.json", DeviceResponse),
        ("feedback_success.json", FeedbackResponse),
        ("account_deletion.json", AccountDeletionResponse),
        ("errors/expired_auth.json", ErrorEnvelope),
    ],
)
def test_fixture_conforms_to_its_contract(file_name: str, model: type[object]) -> None:
    payload = json.loads((FIXTURES / file_name).read_text(encoding="utf-8"))
    model.model_validate(payload)  # type: ignore[attr-defined]


def test_mock_requires_bearer_token_with_the_expired_auth_fixture() -> None:
    response = TestClient(app).get("/v1/feed")

    assert response.status_code == 401
    assert response.json() == json.loads((FIXTURES / "errors/expired_auth.json").read_text())


def test_mock_serves_success_empty_loading_ambiguity_and_merged_scenarios() -> None:
    client = TestClient(app)

    assert client.get("/v1/feed", headers=AUTH_HEADERS).json() == json.loads(
        (FIXTURES / "feed_success.json").read_text()
    )
    assert client.get("/v1/feed?scenario=empty", headers=AUTH_HEADERS).json() == json.loads(
        (FIXTURES / "feed_empty.json").read_text()
    )
    assert client.get("/v1/feed?scenario=loading", headers=AUTH_HEADERS).status_code == 202
    assert client.get("/v1/entities/search?q=Apple", headers=AUTH_HEADERS).json() == json.loads(
        (FIXTURES / "entity_search_ambiguous.json").read_text()
    )
    assert client.get("/v1/events/evt_fremont_initial", headers=AUTH_HEADERS).json() == json.loads(
        (FIXTURES / "event_merged.json").read_text()
    )


def test_openapi_contract_is_exported_from_the_same_models_as_the_mock() -> None:
    contract_path = Path(__file__).resolve().parents[2] / "contracts" / "v1" / "openapi.json"
    contract = json.loads(contract_path.read_text())
    runtime_contract = app.openapi()

    assert contract == runtime_contract


def test_every_v1_mock_route_has_a_response_fixture() -> None:
    manifest = json.loads((FIXTURES / "scenario_manifest.json").read_text())
    fixture_routes = set(manifest["routes"])
    mock_routes = {
        f"{method} {route.path}"
        for route in mock_router.routes
        for method in route.methods or set()
    }

    assert fixture_routes == mock_routes
    assert all((FIXTURES / file_name).is_file() for file_name in manifest["routes"].values())
