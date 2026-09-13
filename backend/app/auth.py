"""Server-side Sign in with Apple and revocable Te Varta session primitives."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

import jwt
import psycopg
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientError

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
ACCESS_TOKEN_TTL = timedelta(hours=1)


class AuthenticationError(ValueError):
    """A token cannot establish or continue an authenticated session."""


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    timezone: str
    session_id: UUID


@dataclass(frozen=True)
class SessionUser:
    internal_id: UUID
    public_id: str
    timezone: str


@dataclass(frozen=True)
class StoredSession:
    id: UUID
    user: SessionUser
    expires_at: datetime
    revoked_at: datetime | None


class AppleIdentityVerifier(Protocol):
    def verify(self, identity_token: str) -> str: ...


class SessionStore(Protocol):
    def get_or_create_user(self, apple_sub: str) -> SessionUser: ...

    def create_session(
        self, user_id: UUID, refresh_token_hash: bytes, expires_at: datetime
    ) -> StoredSession: ...

    def replace_session(
        self, refresh_token_hash: bytes, replacement_hash: bytes, expires_at: datetime
    ) -> StoredSession | None: ...

    def find_active_session(self, session_id: UUID) -> StoredSession | None: ...

    def revoke_session(self, session_id: UUID) -> None: ...


class PostgresSessionStore:
    """Small, parameterized persistence boundary for revocable sessions."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.database_url)

    @staticmethod
    def _user(row: tuple[UUID, str, str]) -> SessionUser:
        return SessionUser(internal_id=row[0], public_id=row[1], timezone=row[2])

    def get_or_create_user(self, apple_sub: str) -> SessionUser:
        # A public-ID collision is extraordinarily unlikely, but retrying keeps the
        # API-facing identifier independent from database conflict handling.
        for _ in range(3):
            try:
                with self._connect() as connection:
                    row = connection.execute(
                        """INSERT INTO users (public_id, apple_sub)
                        VALUES (%s, %s)
                        ON CONFLICT (apple_sub) DO UPDATE SET apple_sub = EXCLUDED.apple_sub
                        RETURNING id, public_id, tz""",
                        (f"usr_{uuid4().hex}", apple_sub),
                    ).fetchone()
                    assert row is not None
                    return self._user(row)
            except psycopg.errors.UniqueViolation:
                continue
        raise AuthenticationError("could not create authenticated user")

    def create_session(
        self, user_id: UUID, refresh_token_hash: bytes, expires_at: datetime
    ) -> StoredSession:
        with self._connect() as connection:
            row = connection.execute(
                """INSERT INTO auth_sessions (user_id, refresh_token_hash, expires_at)
                VALUES (%s, %s, %s) RETURNING id, expires_at, revoked_at""",
                (user_id, refresh_token_hash, expires_at),
            ).fetchone()
            assert row is not None
            user_row = connection.execute(
                "SELECT id, public_id, tz FROM users WHERE id = %s", (user_id,)
            ).fetchone()
            assert user_row is not None
            return StoredSession(row[0], self._user(user_row), row[1], row[2])

    def replace_session(
        self, refresh_token_hash: bytes, replacement_hash: bytes, expires_at: datetime
    ) -> StoredSession | None:
        # One statement consumes the old credential and creates exactly one replacement.
        with self._connect() as connection:
            row = connection.execute(
                """WITH consumed AS (
                    UPDATE auth_sessions
                    SET revoked_at = now()
                    WHERE refresh_token_hash = %s
                      AND revoked_at IS NULL
                      AND expires_at > now()
                    RETURNING user_id
                ), replacement AS (
                    INSERT INTO auth_sessions (user_id, refresh_token_hash, expires_at)
                    SELECT user_id, %s, %s FROM consumed
                    RETURNING id, user_id, expires_at, revoked_at
                )
                SELECT replacement.id, users.id, users.public_id, users.tz,
                       replacement.expires_at, replacement.revoked_at
                FROM replacement JOIN users ON users.id = replacement.user_id""",
                (refresh_token_hash, replacement_hash, expires_at),
            ).fetchone()
            if row is None:
                return None
            return StoredSession(row[0], self._user((row[1], row[2], row[3])), row[4], row[5])

    def find_active_session(self, session_id: UUID) -> StoredSession | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT sessions.id, users.id, users.public_id, users.tz,
                          sessions.expires_at, sessions.revoked_at
                   FROM auth_sessions AS sessions JOIN users ON users.id = sessions.user_id
                   WHERE sessions.id = %s AND sessions.revoked_at IS NULL""",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            return StoredSession(row[0], self._user((row[1], row[2], row[3])), row[4], row[5])

    def revoke_session(self, session_id: UUID) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE auth_sessions SET revoked_at = COALESCE(revoked_at, now()) WHERE id = %s",
                (session_id,),
            )


class AppleJWTVerifier:
    def __init__(self, client_id: str, jwks_url: str = APPLE_JWKS_URL) -> None:
        if not client_id:
            raise AuthenticationError("TEVARTA_APPLE_CLIENT_ID must be configured")
        self.client_id = client_id
        self.jwks_client = PyJWKClient(jwks_url)

    def verify(self, identity_token: str) -> str:
        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(identity_token)
            claims = jwt.decode(
                identity_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.client_id,
                issuer=APPLE_ISSUER,
                options={"require": ["sub", "exp", "iss", "aud"]},
            )
        except (InvalidTokenError, PyJWKClientError) as exc:
            raise AuthenticationError("invalid Apple identity token") from exc
        apple_sub = claims.get("sub")
        if not isinstance(apple_sub, str) or not apple_sub:
            raise AuthenticationError("Apple identity token has no valid subject")
        return apple_sub


class SessionService:
    def __init__(
        self,
        store: SessionStore,
        apple_verifier: AppleIdentityVerifier,
        signing_key: str,
        refresh_ttl: timedelta,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if len(signing_key) < 32:
            raise AuthenticationError("TEVARTA_JWT_SIGNING_KEY must be at least 32 characters")
        if refresh_ttl <= timedelta(0):
            raise AuthenticationError("TEVARTA_REFRESH_TTL_SECONDS must be positive")
        self.store = store
        self.apple_verifier = apple_verifier
        self.signing_key = signing_key
        self.refresh_ttl = refresh_ttl
        self.now = now

    @classmethod
    def from_environ(
        cls, store: SessionStore, environ: Mapping[str, str] | None = None
    ) -> SessionService:
        values = os.environ if environ is None else environ
        try:
            refresh_ttl = timedelta(seconds=int(values["TEVARTA_REFRESH_TTL_SECONDS"]))
            signing_key = values["TEVARTA_JWT_SIGNING_KEY"]
        except KeyError as exc:
            raise AuthenticationError(f"missing required auth setting: {exc.args[0]}") from exc
        return cls(
            store=store,
            apple_verifier=AppleJWTVerifier(values.get("TEVARTA_APPLE_CLIENT_ID", "")),
            signing_key=signing_key,
            refresh_ttl=refresh_ttl,
        )

    def authenticate_apple(self, identity_token: str) -> tuple[str, str, datetime, SessionUser]:
        apple_sub = self.apple_verifier.verify(identity_token)
        user = self.store.get_or_create_user(apple_sub)
        refresh_token = secrets.token_urlsafe(48)
        session = self.store.create_session(
            user.internal_id,
            self._digest_refresh_token(refresh_token),
            self.now() + self.refresh_ttl,
        )
        return self._access_token(session, user), refresh_token, self._access_expiry(), user

    def refresh(self, refresh_token: str) -> tuple[str, str, datetime, SessionUser]:
        replacement = secrets.token_urlsafe(48)
        session = self.store.replace_session(
            self._digest_refresh_token(refresh_token),
            self._digest_refresh_token(replacement),
            self.now() + self.refresh_ttl,
        )
        if session is None:
            raise AuthenticationError("invalid or expired refresh token")
        return (
            self._access_token(session, session.user),
            replacement,
            self._access_expiry(),
            session.user,
        )

    def current_user(self, access_token: str) -> AuthenticatedUser:
        try:
            claims = jwt.decode(
                access_token,
                self.signing_key,
                algorithms=["HS256"],
                options={"require": ["sub", "sid", "exp", "iat", "typ"]},
            )
            if claims["typ"] != "access":
                raise AuthenticationError("wrong token type")
            session_id = UUID(claims["sid"])
        except (InvalidTokenError, KeyError, ValueError) as exc:
            raise AuthenticationError("invalid access token") from exc
        session = self.store.find_active_session(session_id)
        if (
            session is None
            or session.expires_at <= self.now()
            or session.user.public_id != claims["sub"]
        ):
            raise AuthenticationError("session is no longer active")
        return AuthenticatedUser(session.user.public_id, session.user.timezone, session.id)

    def sign_out(self, access_token: str) -> None:
        self.store.revoke_session(self.current_user(access_token).session_id)

    def _access_token(self, session: StoredSession, user: SessionUser) -> str:
        issued_at = self.now()
        return jwt.encode(
            {
                "sub": user.public_id,
                "sid": str(session.id),
                "typ": "access",
                "iat": issued_at,
                "exp": issued_at + ACCESS_TOKEN_TTL,
            },
            self.signing_key,
            algorithm="HS256",
        )

    def _access_expiry(self) -> datetime:
        return self.now() + ACCESS_TOKEN_TTL

    def _digest_refresh_token(self, refresh_token: str) -> bytes:
        return hmac.new(
            self.signing_key.encode(), refresh_token.encode(), hashlib.sha256
        ).digest()
