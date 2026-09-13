"""Fixture-backed implementation of the frozen API surface.

This is deliberately a mock boundary. It exercises the client contract without
pretending to perform Apple verification, graph reads, or database writes.
"""

import json
import os
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.requests import Request

from app.auth import AuthenticationError, SessionService
from app.contracts import (
    AccountDeletionResponse,
    AppleAuthRequest,
    AuthResponse,
    DeviceCreateRequest,
    DeviceResponse,
    EntityResponse,
    EntitySearchResponse,
    ErrorEnvelope,
    EventDetailResponse,
    FeedbackRequest,
    FeedbackResponse,
    FeedResponse,
    GraphResponse,
    LoadingResponse,
    ProfilePatchRequest,
    ProfileResponse,
    RefreshRequest,
    SeedCreateRequest,
    SeedResponse,
    SeedsStatusResponse,
    User,
)

ERROR_RESPONSES = {
    401: {"model": ErrorEnvelope, "description": "Missing, invalid, or expired access token."},
    404: {"model": ErrorEnvelope, "description": "Requested resource was not found."},
    422: {"model": ErrorEnvelope, "description": "Request validation failed."},
}

router = APIRouter(prefix="/v1", responses=ERROR_RESPONSES)
FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "v1"
MOCK_ACCESS_TOKEN = os.getenv("TEVARTA_MOCK_ACCESS_TOKEN", "fixture-access-token")
bearer_scheme = HTTPBearer(auto_error=False)


class FixtureAuthError(Exception):
    """Raised when the fixture server receives a missing or expired bearer token."""


class LiveDataUnavailable(Exception):
    """Raised when a fixture-only route is called after real auth is enabled."""


def fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def error_response(
    status_code: int, code: str, message: str, details: dict[str, str] | None = None
) -> JSONResponse:
    request_id = f"req_{uuid4().hex[:12]}"
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
                "details": details,
            }
        },
        headers={"X-Request-ID": request_id},
    )


def _rate_limit(request: Request, scope: str) -> None:
    limiter = request.app.state.rate_limiter
    host = request.client.host if request.client else "unknown"
    limiter.check(f"{scope}:{host}")


def require_auth_rate_limit(request: Request) -> None:
    _rate_limit(request, "auth")


def authenticated_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> User:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise FixtureAuthError
    service: SessionService | None = getattr(request.app.state, "auth_service", None)
    if service is None:
        if credentials.credentials != MOCK_ACCESS_TOKEN:
            raise FixtureAuthError
        return User(id="usr_fixture", timezone="America/New_York")
    try:
        current = service.current_user(credentials.credentials)
    except AuthenticationError as exc:
        raise FixtureAuthError from exc
    return User(id=current.id, timezone=current.timezone)


def require_fixture_user(request: Request, user: User = Depends(authenticated_user)) -> User:
    # T25/T32 replace these fixture handlers with persisted, user-scoped reads.
    # Never report another account's fixture data as a real authenticated result.
    if getattr(request.app.state, "auth_service", None) is not None:
        raise LiveDataUnavailable
    return user


@router.post("/auth/apple", response_model=AuthResponse, tags=["authentication"])
def authenticate_with_apple(request: Request, body: AppleAuthRequest) -> dict[str, object]:
    service: SessionService | None = getattr(request.app.state, "auth_service", None)
    if service is None:
        return fixture("auth_success.json")
    _rate_limit(request, "auth")
    access_token, refresh_token, expires_at, user = service.authenticate_apple(body.identity_token)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_at": expires_at,
        "user": {"id": user.public_id, "timezone": user.timezone},
    }


@router.post(
    "/auth/refresh",
    response_model=AuthResponse,
    tags=["authentication"],
    summary="SC3 proposal: exchange a refresh token for a short-lived session",
)
def refresh_session(request: Request, body: RefreshRequest) -> dict[str, object]:
    service: SessionService | None = getattr(request.app.state, "auth_service", None)
    if service is None:
        return fixture("auth_success.json")
    _rate_limit(request, "auth")
    access_token, refresh_token, expires_at, user = service.refresh(body.refresh_token)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_at": expires_at,
        "user": {"id": user.public_id, "timezone": user.timezone},
    }


@router.post(
    "/auth/sign-out",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["authentication"],
    summary="SC3 proposal: revoke the current session",
)
def sign_out(
    request: Request,
    _: None = Depends(require_auth_rate_limit),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    __: User = Depends(authenticated_user),
) -> Response:
    service: SessionService | None = getattr(request.app.state, "auth_service", None)
    if service is not None:
        assert credentials is not None
        service.sign_out(credentials.credentials)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/account",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AccountDeletionResponse,
    tags=["account"],
    summary="SC3 proposal: schedule deletion of user-owned data",
)
def delete_account(_: User = Depends(require_fixture_user)) -> dict[str, object]:
    return fixture("account_deletion.json")


@router.get("/entities/search", response_model=EntitySearchResponse, tags=["entities"])
def search_entities(
    q: str = Query(min_length=1, max_length=128), _: User = Depends(require_fixture_user)
) -> dict[str, object]:
    if q.casefold() == "apple":
        return fixture("entity_search_ambiguous.json")
    return fixture("entity_search_success.json")


@router.post(
    "/seeds",
    response_model=SeedResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["seeds"],
)
def create_seed(
    _: SeedCreateRequest, __: User = Depends(require_fixture_user)
) -> dict[str, object]:
    return fixture("seed_success.json")


@router.get("/seeds/status", response_model=SeedsStatusResponse, tags=["seeds"])
def seeds_status(_: User = Depends(require_fixture_user)) -> dict[str, object]:
    return fixture("seeds_status.json")


@router.get(
    "/feed",
    response_model=FeedResponse,
    responses={202: {"model": LoadingResponse, "description": "Fixture-only loading state."}},
    tags=["feed"],
)
def get_feed(
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=50),
    scenario: str | None = Query(default=None),
    _: User = Depends(require_fixture_user),
) -> dict[str, object] | JSONResponse:
    del cursor, limit
    if scenario == "loading":
        return JSONResponse(status_code=202, content=fixture("loading.json"))
    if scenario == "empty":
        return fixture("feed_empty.json")
    return fixture("feed_success.json")


@router.get("/events/{event_id}", response_model=EventDetailResponse, tags=["events"])
def get_event(event_id: str, _: User = Depends(require_fixture_user)) -> dict[str, object]:
    if event_id == "evt_fremont_initial":
        return fixture("event_merged.json")
    return fixture("event_success.json")


@router.get("/graph", response_model=GraphResponse, tags=["graph"])
def get_graph(_: User = Depends(require_fixture_user)) -> dict[str, object]:
    return fixture("graph.json")


@router.get("/entities/{entity_id}", response_model=EntityResponse, tags=["entities"])
def get_entity(entity_id: str, _: User = Depends(require_fixture_user)) -> dict[str, object]:
    del entity_id
    return fixture("entity_success.json")


@router.delete("/seeds/{entity_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["seeds"])
def delete_seed(entity_id: str, _: User = Depends(require_fixture_user)) -> Response:
    del entity_id
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/profile", response_model=ProfileResponse, tags=["profile"])
def get_profile(_: User = Depends(require_fixture_user)) -> dict[str, object]:
    return fixture("profile.json")


@router.patch("/profile", response_model=ProfileResponse, tags=["profile"])
def patch_profile(
    _: ProfilePatchRequest, __: User = Depends(require_fixture_user)
) -> dict[str, object]:
    return fixture("profile.json")


@router.post(
    "/devices", response_model=DeviceResponse, status_code=status.HTTP_201_CREATED, tags=["devices"]
)
def register_device(
    _: DeviceCreateRequest, __: User = Depends(require_fixture_user)
) -> dict[str, object]:
    return fixture("device_success.json")


@router.post(
    "/feedback",
    response_model=FeedbackResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["feedback"],
)
def record_feedback(
    request: Request, _: FeedbackRequest, __: User = Depends(require_fixture_user)
) -> dict[str, object]:
    _rate_limit(request, "feedback")
    return fixture("feedback_success.json")


def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    fields = {"field": ".".join(str(part) for part in error["loc"]) for error in exc.errors()}
    return error_response(422, "invalid_request", "The request is invalid.", fields)
