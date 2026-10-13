import os

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app.auth import AuthenticationError, PostgresSessionStore, SessionService
from app.mock_api import (
    FixtureAuthError,
    LiveDataUnavailable,
    error_response,
    fixture,
    validation_error_handler,
)
from app.mock_api import router as mock_router
from app.operations import (
    EnvironmentConfigurationError,
    EnvironmentSettings,
    FixedWindowRateLimiter,
    RateLimitExceeded,
    install_log_redaction,
    install_transport_security,
)
from app.telemetry import MemoryTelemetryStore, PostgresTelemetryStore, TelemetryService

app = FastAPI(
    title="Te Varta API",
    version="1.0.0-draft",
    description=(
        "Fixture-backed API surface for T03. Real authentication, persistence, and graph "
        "work are intentionally deferred to their assigned tasks."
    ),
)
app.include_router(mock_router)

# Validate the environment boundary at process start, before an API request can
# reach a data service.  Production additionally refuses clear-text HTTP.
settings = EnvironmentSettings.from_environ()
auth_settings = (
    "TEVARTA_APPLE_CLIENT_ID",
    "TEVARTA_JWT_SIGNING_KEY",
    "TEVARTA_REFRESH_TTL_SECONDS",
)
configured_auth = [name for name in auth_settings if os.getenv(name)]
if configured_auth and len(configured_auth) != len(auth_settings):
    raise EnvironmentConfigurationError("configure every T11 auth setting or none of them")
if settings.name == "production" and len(configured_auth) != len(auth_settings):
    raise EnvironmentConfigurationError("production requires configured T11 authentication")
app.state.auth_service = (
    SessionService.from_environ(PostgresSessionStore(settings.postgres_url))
    if len(configured_auth) == len(auth_settings)
    else None
)
app.state.rate_limiter = FixedWindowRateLimiter(limit=10, window_seconds=60)
app.state.telemetry_service = (
    TelemetryService(PostgresTelemetryStore(settings.postgres_url))
    if app.state.auth_service is not None
    else TelemetryService(MemoryTelemetryStore())
)
install_transport_security(app, settings)

install_log_redaction()


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    return validation_error_handler(_, exc)


@app.exception_handler(FixtureAuthError)
async def handle_expired_auth(_: Request, __: FixtureAuthError) -> JSONResponse:
    # The fixture supplies the exact client-visible wording and envelope.
    return JSONResponse(status_code=401, content=fixture("errors/expired_auth.json"))


@app.exception_handler(AuthenticationError)
async def handle_authentication_error(_: Request, exc: AuthenticationError) -> JSONResponse:
    return error_response(401, "invalid_auth", "Authentication failed.")


@app.exception_handler(RateLimitExceeded)
async def handle_rate_limit(_: Request, __: RateLimitExceeded) -> JSONResponse:
    return error_response(429, "rate_limited", "Too many requests. Try again shortly.")


@app.exception_handler(LiveDataUnavailable)
async def handle_fixture_route_in_live_mode(_: Request, __: LiveDataUnavailable) -> JSONResponse:
    return error_response(
        501, "not_implemented", "This authenticated endpoint is not available yet."
    )


@app.get("/health", tags=["operations"])
def health() -> dict[str, str]:
    return {"status": "ok"}
