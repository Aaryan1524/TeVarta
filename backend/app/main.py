from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app.mock_api import FixtureAuthError, fixture, validation_error_handler
from app.mock_api import router as mock_router

app = FastAPI(
    title="Te Varta API",
    version="1.0.0-draft",
    description=(
        "Fixture-backed API surface for T03. Real authentication, persistence, and graph "
        "work are intentionally deferred to their assigned tasks."
    ),
)
app.include_router(mock_router)


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
