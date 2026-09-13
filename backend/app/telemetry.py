"""T12 telemetry validation, idempotency, and rate-limit primitives."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Protocol

import psycopg

from app.contracts import FeedbackRequest


class RateLimitExceeded(RuntimeError):
    pass


class UnknownEvent(RuntimeError):
    pass


@dataclass(frozen=True)
class TelemetryResult:
    accepted: bool
    deduplicated: bool


class MemoryTelemetryStore:
    """Deterministic adapter used by fixture-mode tests; Postgres owns production persistence."""

    def __init__(self) -> None:
        self.keys: set[tuple[str, str]] = set()

    def record(self, user_id: str, event: FeedbackRequest) -> bool:
        key = (user_id, event.idempotency_key)
        if key in self.keys:
            return False
        self.keys.add(key)
        return True


class TelemetryStore(Protocol):
    def record(self, user_id: str, event: FeedbackRequest) -> bool: ...


class PostgresTelemetryStore:
    """Persist telemetry atomically; duplicate retry keys return no inserted row."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def record(self, user_id: str, event: FeedbackRequest) -> bool:
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO feedback
                        (user_id, event_id, action, slot_type, slot_position,
                         session_duration_s, idempotency_key, ts)
                    SELECT users.id, events.id, %(action)s, %(slot_type)s, %(slot_position)s,
                           %(session_duration_s)s, %(idempotency_key)s, %(occurred_at)s
                    FROM users
                    LEFT JOIN events ON events.public_id = %(event_id)s
                    WHERE users.public_id = %(user_id)s
                    ON CONFLICT (user_id, idempotency_key) DO NOTHING
                    RETURNING id
                    """,
                    {
                        "user_id": user_id,
                        "event_id": event.event_id,
                        "action": event.action,
                        "slot_type": event.slot_type,
                        "slot_position": event.slot_position,
                        "session_duration_s": event.session_duration_s,
                        "idempotency_key": event.idempotency_key,
                        "occurred_at": event.occurred_at,
                    },
                )
                if cursor.fetchone() is not None:
                    return True
                cursor.execute("SELECT 1 FROM users WHERE public_id = %s", (user_id,))
                if cursor.fetchone() is None:
                    raise PermissionError("authenticated user does not exist")
                if event.event_id is not None:
                    cursor.execute("SELECT 1 FROM events WHERE public_id = %s", (event.event_id,))
                    if cursor.fetchone() is None:
                        raise UnknownEvent("event does not exist")
                return False


class UserRateLimiter:
    def __init__(self, limit: int = 60, window: timedelta = timedelta(minutes=1)) -> None:
        self.limit = limit
        self.window = window
        self._lock = Lock()
        self._entries: dict[str, list[datetime]] = {}

    def check(self, user_id: str, now: datetime) -> None:
        with self._lock:
            entries = [time for time in self._entries.get(user_id, []) if time > now - self.window]
            if len(entries) >= self.limit:
                raise RateLimitExceeded("feedback rate limit exceeded")
            entries.append(now)
            self._entries[user_id] = entries


class TelemetryService:
    def __init__(
        self,
        store: TelemetryStore,
        limiter: UserRateLimiter | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.limiter = limiter or UserRateLimiter()
        self.now = now

    def record(self, user_id: str, event: FeedbackRequest) -> TelemetryResult:
        self.limiter.check(user_id, self.now())
        inserted = self.store.record(user_id, event)
        return TelemetryResult(accepted=True, deduplicated=not inserted)
