from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import pytest
from app.auth import (
    APPLE_ISSUER,
    AppleJWTVerifier,
    AuthenticationError,
    SessionService,
    SessionUser,
    StoredSession,
)
from cryptography.hazmat.primitives.asymmetric import rsa

SIGNING_KEY = "a" * 32
NOW = datetime.now(UTC)


class FakeAppleVerifier:
    def __init__(self, apple_sub: str = "apple-sub") -> None:
        self.apple_sub = apple_sub

    def verify(self, _: str) -> str:
        return self.apple_sub


class MemoryStore:
    def __init__(self) -> None:
        self.user = SessionUser(uuid4(), "usr_test", "UTC")
        self.sessions: dict[UUID, StoredSession] = {}
        self.refresh_index: dict[bytes, UUID] = {}

    def get_or_create_user(self, apple_sub: str) -> SessionUser:
        assert apple_sub == "apple-sub"
        return self.user

    def create_session(
        self, user_id: UUID, refresh_token_hash: bytes, expires_at: datetime
    ) -> StoredSession:
        assert user_id == self.user.internal_id
        session = StoredSession(uuid4(), self.user, expires_at, None)
        self.sessions[session.id] = session
        self.refresh_index[refresh_token_hash] = session.id
        return session

    def find_session(self, refresh_token_hash: bytes) -> StoredSession | None:
        session_id = self.refresh_index.get(refresh_token_hash)
        return self.sessions.get(session_id) if session_id else None

    def find_active_session(self, session_id: UUID) -> StoredSession | None:
        session = self.sessions.get(session_id)
        return session if session and session.revoked_at is None else None

    def revoke_session(self, session_id: UUID) -> None:
        session = self.sessions[session_id]
        self.sessions[session_id] = StoredSession(
            session.id, session.user, session.expires_at, NOW
        )


def service(store: MemoryStore | None = None) -> tuple[SessionService, MemoryStore]:
    store = store or MemoryStore()
    return (
        SessionService(
            store,
            FakeAppleVerifier(),
            SIGNING_KEY,
            timedelta(days=30),
            now=lambda: NOW,
        ),
        store,
    )


def test_apple_auth_uses_verified_subject_and_access_token_expires_within_one_hour() -> None:
    auth, _ = service()

    access_token, refresh_token, expires_at, user = auth.authenticate_apple("client-supplied-token")

    assert user.public_id == "usr_test"
    assert refresh_token
    assert expires_at - NOW == timedelta(hours=1)
    assert auth.current_user(access_token).id == "usr_test"


def test_refresh_rotates_the_refresh_token_and_revokes_the_old_session() -> None:
    auth, _ = service()
    access_token, refresh_token, _, _ = auth.authenticate_apple("identity")

    replacement_access, replacement_refresh, _, _ = auth.refresh(refresh_token)

    assert replacement_refresh != refresh_token
    with pytest.raises(AuthenticationError):
        auth.refresh(refresh_token)
    with pytest.raises(AuthenticationError):
        auth.current_user(access_token)
    assert auth.current_user(replacement_access).id == "usr_test"


def test_sign_out_revokes_access_and_refresh_credentials() -> None:
    auth, _ = service()
    access_token, refresh_token, _, _ = auth.authenticate_apple("identity")

    auth.sign_out(access_token)

    with pytest.raises(AuthenticationError):
        auth.current_user(access_token)
    with pytest.raises(AuthenticationError):
        auth.refresh(refresh_token)


def test_forged_access_token_subject_cannot_access_another_user() -> None:
    auth, _ = service()
    access_token, _, _, _ = auth.authenticate_apple("identity")
    claims = jwt.decode(access_token, SIGNING_KEY, algorithms=["HS256"])
    claims["sub"] = "usr_other"
    forged = jwt.encode(claims, SIGNING_KEY, algorithm="HS256")

    with pytest.raises(AuthenticationError):
        auth.current_user(forged)


class SigningKey:
    def __init__(self, key: object) -> None:
        self.key = key


class StaticJWKClient:
    def __init__(self, key: object) -> None:
        self.key = key

    def get_signing_key_from_jwt(self, _: str) -> SigningKey:
        return SigningKey(self.key)


def apple_token(private_key: object, **overrides: object) -> str:
    claims = {
        "sub": "apple-sub",
        "iss": APPLE_ISSUER,
        "aud": "com.tevarta.beta",
        "exp": NOW + timedelta(minutes=5),
    } | overrides
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test"})


def verifier() -> tuple[AppleJWTVerifier, object]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    apple_verifier = AppleJWTVerifier("com.tevarta.beta")
    apple_verifier.jwks_client = StaticJWKClient(private_key.public_key())
    return apple_verifier, private_key


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "wrong.audience"},
        {"iss": "https://forged.example"},
        {"exp": NOW - timedelta(minutes=1)},
    ],
)
def test_apple_verifier_rejects_wrong_audience_issuer_or_expiry(claims: dict[str, object]) -> None:
    apple_verifier, private_key = verifier()

    with pytest.raises(AuthenticationError):
        apple_verifier.verify(apple_token(private_key, **claims))


def test_apple_verifier_rejects_a_token_signed_by_an_unknown_key() -> None:
    apple_verifier, _ = verifier()
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    with pytest.raises(AuthenticationError):
        apple_verifier.verify(apple_token(attacker_key))
