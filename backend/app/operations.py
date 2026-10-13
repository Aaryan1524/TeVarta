"""Operational safety controls shared by API and worker entry points.

This module deliberately contains no provider credentials.  It makes the
environment boundary, redaction rules, and cost caps executable before the
production integrations that consume them are added in later tasks.
"""

from __future__ import annotations

import copy
import logging
import os
import re
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from threading import Lock
from time import monotonic
from typing import Any

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.requests import Request
from starlette.responses import Response
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

SENSITIVE_FIELD_NAMES = frozenset(
    {
        "access_token", "refresh_token", "identity_token", "authorization_code", "apns_token",
        "apple_sub", "authorization", "cookie", "set-cookie", "tevarta_jwt_signing_key",
    }
)
REDACTED = "[REDACTED]"


class EnvironmentConfigurationError(ValueError):
    """Raised when runtime configuration could cross environment boundaries."""


class SearchBudgetExceeded(RuntimeError):
    """Raised before a search provider is called beyond its monthly allocation."""


class RateLimitExceeded(RuntimeError):
    """Raised before a sensitive endpoint receives more than its small allowance."""


class FixedWindowRateLimiter:
    """Process-local safety limit; deployed instances must use a shared edge limit."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._lock = Lock()
        self._attempts: dict[str, deque[float]] = {}

    def check(self, key: str, now: float | None = None) -> None:
        current = monotonic() if now is None else now
        with self._lock:
            attempts = self._attempts.setdefault(key, deque())
            while attempts and attempts[0] <= current - self.window_seconds:
                attempts.popleft()
            if len(attempts) >= self.limit:
                raise RateLimitExceeded("rate limit exceeded")
            attempts.append(current)


@dataclass(frozen=True)
class EnvironmentSettings:
    name: str
    credential_set: str
    postgres_url: str
    neo4j_url: str
    r2_bucket: str

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> EnvironmentSettings:
        values = os.environ if environ is None else environ
        name = values.get("TEVARTA_ENV", "development")
        credential_set = values.get("TEVARTA_CREDENTIAL_SET", "development")
        postgres_url = values.get("TEVARTA_POSTGRES_URL", "postgresql://localhost/tevarta_dev")
        neo4j_url = values.get("TEVARTA_NEO4J_URL", "neo4j://localhost:7687")
        r2_bucket = values.get("TEVARTA_R2_BUCKET", "tevarta-dev")
        settings = cls(name, credential_set, postgres_url, neo4j_url, r2_bucket)
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.name not in {"development", "production"}:
            raise EnvironmentConfigurationError("TEVARTA_ENV must be development or production")
        if self.credential_set != self.name:
            raise EnvironmentConfigurationError(
                "TEVARTA_CREDENTIAL_SET must exactly match TEVARTA_ENV; do not share credentials"
            )
        if self.name == "development" and any(
            "prod" in value.casefold()
            for value in (self.postgres_url, self.neo4j_url, self.r2_bucket)
        ):
            raise EnvironmentConfigurationError(
                "development must not reference production data services"
            )
        if self.name == "production" and any(
            marker in value.casefold()
            for value in (self.postgres_url, self.neo4j_url, self.r2_bucket)
            for marker in ("localhost", "dev", "development")
        ):
            raise EnvironmentConfigurationError(
                "production must not reference development data services"
            )


def redact_value(value: Any, field_name: str | None = None) -> Any:
    """Return a deep-copied value with beta-sensitive identifiers removed."""
    if field_name and field_name.casefold() in SENSITIVE_FIELD_NAMES:
        return REDACTED
    if isinstance(value, Mapping):
        return {key: redact_value(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, str):
        value = re.sub(r"(?i)(bearer\s+)[^\s,]+", r"\1" + REDACTED, value)
        value = re.sub(
            r"(?i)((?:[\"']?)(?:access_token|refresh_token|identity_token|authorization_code|"
            r"apns_token|apple_sub|tevarta_jwt_signing_key)(?:[\"']?)\s*[:=]\s*(?:[\"']?))"
            r"[^\s,}&\"']+",
            r"\1" + REDACTED,
            value,
        )
    return value


class SensitiveDataRedactionFilter(logging.Filter):
    """Redact message text and structured extras before any configured handler sees them."""

    def filter(self, record: logging.LogRecord) -> bool:
        redacted = redact_value(record.getMessage())
        msg, args = redact_value(record.msg), redact_value(record.args)
        try:
            rendered = str(msg) % args if args else str(msg)
        except (TypeError, ValueError, KeyError):
            rendered = None
        # Keep the template and arguments when redacting them separately renders exactly
        # the whole-message redaction: uvicorn's access formatter unpacks record.args.
        # A secret split across template and argument ("access_token=%s") differs, so
        # it falls back to the flattened, fully redacted message.
        if rendered == redacted:
            record.msg, record.args = msg, args
        else:
            record.msg, record.args = redacted, ()
        for key, value in tuple(record.__dict__.items()):
            if key not in {"msg", "args"}:
                record.__dict__[key] = redact_value(value, key)
        return True


class HSTSMiddleware(BaseHTTPMiddleware):
    """Set the beta HSTS policy on production HTTPS responses."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


def install_transport_security(
    app: Starlette, settings: EnvironmentSettings, environ: Mapping[str, str] | None = None
) -> None:
    """Enforce HTTPS in production, correctly behind a TLS-terminating proxy.

    A hosted API usually receives plain HTTP from its load balancer. Without trusted
    X-Forwarded-* handling, the HTTPS redirect loops forever and every request
    appears to come from the proxy's IP, so per-IP auth rate limits would throttle
    all users together. Only proxies listed in TEVARTA_TRUSTED_PROXIES (IPs or CIDR
    ranges; default loopback) may set the scheme and client address.
    """
    if settings.name != "production":
        return
    values = os.environ if environ is None else environ
    trusted = [
        host.strip()
        for host in values.get("TEVARTA_TRUSTED_PROXIES", "127.0.0.1").split(",")
        if host.strip()
    ]
    if "*" in trusted:
        raise EnvironmentConfigurationError(
            "TEVARTA_TRUSTED_PROXIES must list proxy addresses; '*' lets clients spoof HTTPS"
        )
    app.add_middleware(HTTPSRedirectMiddleware)
    app.add_middleware(HSTSMiddleware)
    # Added last, so outermost: the scheme is corrected before the redirect check.
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted)


def install_log_redaction() -> None:
    """Attach redaction to the API and worker logger paths configured at startup."""
    for logger_name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "celery"):
        logger = logging.getLogger(logger_name)
        logger.addFilter(SensitiveDataRedactionFilter())
        for handler in logger.handlers:
            handler.addFilter(SensitiveDataRedactionFilter())


def scrub_sentry_event(event: dict[str, Any], _: dict[str, Any] | None = None) -> dict[str, Any]:
    """Sentry ``before_send`` hook that removes request headers and bodies."""
    scrubbed = copy.deepcopy(event)
    request = scrubbed.get("request")
    if isinstance(request, dict):
        request["headers"] = REDACTED
        request["data"] = REDACTED
        request["cookies"] = REDACTED
    return redact_value(scrubbed)


@dataclass(frozen=True)
class UsageRecord:
    component: str
    quantity: int
    unit: str
    estimated_cost_usd: Decimal


class SearchUsageLedger:
    """Thread-safe cap enforcement used before search work is dispatched."""

    def __init__(
        self,
        monthly_query_cap: int,
        alert_ratio: Decimal = Decimal("0.80"),
        alert: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if monthly_query_cap < 1:
            raise ValueError("monthly_query_cap must be positive")
        self.monthly_query_cap = monthly_query_cap
        self.alert_ratio = alert_ratio
        self.alert = alert or (lambda _: None)
        self._lock = Lock()
        self._records: list[UsageRecord] = []
        self._alerted = False

    def reserve_search_query(self, estimated_cost_usd: Decimal = Decimal("0")) -> UsageRecord:
        with self._lock:
            used = self.search_queries_used
            if used + 1 > self.monthly_query_cap:
                self.alert(
                    {"event": "search_cap_breached", "used": used, "cap": self.monthly_query_cap}
                )
                raise SearchBudgetExceeded("monthly search query cap reached")
            record = UsageRecord("search", 1, "query", estimated_cost_usd)
            self._records.append(record)
            if not self._alerted and Decimal(self.search_queries_used) >= (
                Decimal(self.monthly_query_cap) * self.alert_ratio
            ):
                self._alerted = True
                self.alert(
                    {
                        "event": "search_cap_80_percent",
                        "used": self.search_queries_used,
                        "cap": self.monthly_query_cap,
                    }
                )
            return record

    def record(
        self, component: str, quantity: int, unit: str, estimated_cost_usd: Decimal
    ) -> UsageRecord:
        if quantity < 0 or estimated_cost_usd < 0:
            raise ValueError("usage quantities and costs cannot be negative")
        with self._lock:
            record = UsageRecord(component, quantity, unit, estimated_cost_usd)
            self._records.append(record)
            return record

    @property
    def search_queries_used(self) -> int:
        return sum(record.quantity for record in self._records if record.component == "search")

    def report(self) -> dict[str, dict[str, str | int]]:
        summary: dict[str, dict[str, Decimal | int]] = {}
        for record in self._records:
            entry = summary.setdefault(
                record.component, {"quantity": 0, "estimated_cost_usd": Decimal("0")}
            )
            entry["quantity"] += record.quantity
            entry["estimated_cost_usd"] += record.estimated_cost_usd
        return {
            component: {
                "quantity": values["quantity"],
                "estimated_cost_usd": str(values["estimated_cost_usd"]),
            }
            for component, values in summary.items()
        }
