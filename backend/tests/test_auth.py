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
    apple_nonce_claim,
)
from app.main import app
from app.operations import FixedWindowRateLimiter, RateLimitExceeded
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

SIGNING_KEY = "a" * 32
NOW = datetime.now(UTC)


class FakeAppleVerifier:
    def __init__(self, apple_sub: str = "apple-sub") -> None:
        self.apple_sub = apple_sub

    def verify(self, _: str, nonce: str | None = None) -> str:
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

    def replace_session(
        self, refresh_token_hash: bytes, replacement_hash: bytes, expires_at: datetime
    ) -> StoredSession | None:
        session_id = self.refresh_index.get(refresh_token_hash)
        session = self.sessions.get(session_id) if session_id else None
        if session is None or session.revoked_at is not None or session.expires_at <= NOW:
            return None
        self.revoke_session(session.id)
        return self.create_session(session.user.internal_id, replacement_hash, expires_at)

    def find_active_session(self, session_id: UUID) -> StoredSession | None:
        session = self.sessions.get(session_id)
        return session if session and session.revoked_at is None else None

    def revoke_session(self, session_id: UUID) -> None:
        session = self.sessions[session_id]
        self.sessions[session_id] = StoredSession(
            session.id, session.user, session.expires_at, NOW
        )


class MultiUserStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.users = {
            "apple-sub-a": SessionUser(uuid4(), "usr_alice", "America/New_York"),
            "apple-sub-b": SessionUser(uuid4(), "usr_bob", "Europe/London"),
        }

    def get_or_create_user(self, apple_sub: str) -> SessionUser:
        return self.users[apple_sub]

    def create_session(
        self, user_id: UUID, refresh_token_hash: bytes, expires_at: datetime
    ) -> StoredSession:
        user = next(user for user in self.users.values() if user.internal_id == user_id)
        session = StoredSession(uuid4(), user, expires_at, None)
        self.sessions[session.id] = session
        self.refresh_index[refresh_token_hash] = session.id
        return session


class TokenMappedAppleVerifier:
    def verify(self, identity_token: str, nonce: str | None = None) -> str:
        return {"alice-token": "apple-sub-a", "bob-token": "apple-sub-b"}[identity_token]


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


def test_real_mode_routes_issue_and_revoke_credentials_through_the_common_dependency() -> None:
    auth, _ = service()
    previous_service = app.state.auth_service
    previous_limiter = app.state.rate_limiter
    app.state.auth_service = auth
    app.state.rate_limiter = FixedWindowRateLimiter(limit=10, window_seconds=60)
    try:
        client = TestClient(app)
        signed_in = client.post("/v1/auth/apple", json={"identity_token": "verified-token"})
        assert signed_in.status_code == 200
        access_token = signed_in.json()["access_token"]
        refresh_token = signed_in.json()["refresh_token"]
        headers = {"Authorization": f"Bearer {access_token}"}
        profile = client.get("/v1/profile", headers=headers)
        assert profile.status_code == 200
        assert profile.json()["profile"]["timezone"] == "UTC"
        assert client.post("/v1/auth/sign-out", headers=headers).status_code == 204
        assert client.get("/v1/profile", headers=headers).status_code == 401
        refreshed = client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})
        assert refreshed.status_code == 401
    finally:
        app.state.auth_service = previous_service
        app.state.rate_limiter = previous_limiter


def test_second_user_cannot_read_the_first_users_profile() -> None:
    store = MultiUserStore()
    auth = SessionService(
        store,
        TokenMappedAppleVerifier(),
        SIGNING_KEY,
        timedelta(days=30),
        now=lambda: NOW,
    )
    previous_service = app.state.auth_service
    previous_limiter = app.state.rate_limiter
    app.state.auth_service = auth
    app.state.rate_limiter = FixedWindowRateLimiter(limit=10, window_seconds=60)
    try:
        client = TestClient(app)
        alice = client.post("/v1/auth/apple", json={"identity_token": "alice-token"})
        bob = client.post("/v1/auth/apple", json={"identity_token": "bob-token"})
        assert alice.status_code == bob.status_code == 200

        alice_profile = client.get(
            "/v1/profile", headers={"Authorization": f"Bearer {alice.json()['access_token']}"}
        )
        bob_profile = client.get(
            "/v1/profile", headers={"Authorization": f"Bearer {bob.json()['access_token']}"}
        )

        assert alice_profile.status_code == bob_profile.status_code == 200
        assert alice_profile.json()["profile"]["timezone"] == "America/New_York"
        assert bob_profile.json()["profile"]["timezone"] == "Europe/London"
    finally:
        app.state.auth_service = previous_service
        app.state.rate_limiter = previous_limiter


def test_rate_limiter_rejects_the_request_after_its_allowance() -> None:
    limiter = FixedWindowRateLimiter(limit=2, window_seconds=60)
    limiter.check("auth:127.0.0.1", now=10)
    limiter.check("auth:127.0.0.1", now=11)
    with pytest.raises(RateLimitExceeded):
        limiter.check("auth:127.0.0.1", now=12)


NONCE = "client-generated-nonce-0123456789"


def test_apple_verifier_accepts_a_token_bound_to_the_client_nonce() -> None:
    apple_verifier, private_key = verifier()

    token = apple_token(private_key, nonce=apple_nonce_claim(NONCE))

    assert apple_verifier.verify(token, nonce=NONCE)


@pytest.mark.parametrize(
    "claims", [{"nonce": apple_nonce_claim("a-different-nonce-value")}, {"nonce": NONCE}, {}]
)
def test_apple_verifier_rejects_a_missing_raw_or_mismatched_nonce(
    claims: dict[str, object],
) -> None:
    apple_verifier, private_key = verifier()

    with pytest.raises(AuthenticationError, match="nonce"):
        apple_verifier.verify(apple_token(private_key, **claims), nonce=NONCE)


def test_required_nonce_rejects_sign_in_without_one() -> None:
    apple_verifier, private_key = verifier()
    apple_verifier.require_nonce = True

    with pytest.raises(AuthenticationError, match="nonce is required"):
        apple_verifier.verify(apple_token(private_key))
    assert apple_verifier.verify(
        apple_token(private_key, nonce=apple_nonce_claim(NONCE)), nonce=NONCE
    )


def test_sign_in_route_passes_the_nonce_to_verification() -> None:
    class RecordingVerifier(FakeAppleVerifier):
        nonces: list[str | None] = []

        def verify(self, _: str, nonce: str | None = None) -> str:
            self.nonces.append(nonce)
            return self.apple_sub

    verifier_ = RecordingVerifier()
    auth = SessionService(MemoryStore(), verifier_, SIGNING_KEY, timedelta(days=30),
                          now=lambda: NOW)
    previous = app.state.auth_service
    app.state.auth_service = auth
    try:
        response = TestClient(app).post(
            "/v1/auth/apple", json={"identity_token": "verified-token", "nonce": NONCE}
        )
    finally:
        app.state.auth_service = previous

    assert response.status_code == 200
    assert verifier_.nonces == [NONCE]
