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
from uuid import UUID

import jwt
from jwt import InvalidTokenError, PyJWKClient

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

    def find_session(self, refresh_token_hash: bytes) -> StoredSession | None: ...

    def find_active_session(self, session_id: UUID) -> StoredSession | None: ...

    def revoke_session(self, session_id: UUID) -> None: ...


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
        except InvalidTokenError as exc:
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
        session = self.store.find_session(self._digest_refresh_token(refresh_token))
        if session is None or session.revoked_at is not None or session.expires_at <= self.now():
            raise AuthenticationError("invalid or expired refresh token")
        self.store.revoke_session(session.id)
        replacement = secrets.token_urlsafe(48)
        renewed = self.store.create_session(
            session.user.internal_id,
            self._digest_refresh_token(replacement),
            self.now() + self.refresh_ttl,
        )
        return (
            self._access_token(renewed, session.user),
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
