from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.requests import Request

from app.mock_api import FixtureAuthError, fixture, validation_error_handler
from app.mock_api import router as mock_router
from app.operations import EnvironmentSettings, HSTSMiddleware, install_log_redaction

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
if settings.name == "production":
    app.add_middleware(HTTPSRedirectMiddleware)
    app.add_middleware(HSTSMiddleware)

install_log_redaction()


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    return validation_error_handler(_, exc)


@app.exception_handler(FixtureAuthError)
async def handle_expired_auth(_: Request, __: FixtureAuthError) -> JSONResponse:
    # The fixture supplies the exact client-visible wording and envelope.
    return JSONResponse(status_code=401, content=fixture("errors/expired_auth.json"))


@app.get("/health", tags=["operations"])
def health() -> dict[str, str]:
    return {"status": "ok"}
