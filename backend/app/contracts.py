"""Pydantic models for the T03 v1 draft contract.

These models deliberately describe the API boundary, not database rows. T05 and
later tasks may add persistence behind this interface without changing it.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorBody(StrictModel):
    code: str
    message: str
    request_id: str
    details: dict[str, str] | None = None


class ErrorEnvelope(StrictModel):
    error: ErrorBody


class User(StrictModel):
    id: str = Field(pattern=r"^usr_[A-Za-z0-9_-]+$")
    timezone: str


class AppleAuthRequest(StrictModel):
    identity_token: str = Field(min_length=1)
    authorization_code: str | None = None


class RefreshRequest(StrictModel):
    refresh_token: str = Field(min_length=1)


class AuthResponse(StrictModel):
    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"]
    expires_at: datetime
    user: User


class EntityCandidate(StrictModel):
    id: str = Field(pattern=r"^ent_[A-Za-z0-9_-]+$")
    name: str
    entity_type: Literal["company", "place", "commodity", "regulator", "security"]
    ticker: str | None = None
    lifecycle: Literal["catalog", "live"]


class EntitySearchResponse(StrictModel):
    query: str
    candidates: list[EntityCandidate]
    ambiguous: bool


class SeedCreateRequest(StrictModel):
    entity_id: str = Field(pattern=r"^ent_[A-Za-z0-9_-]+$")
    weight: float = Field(ge=0, le=1)
    source: Literal["manual", "broker"] = "manual"


class Seed(StrictModel):
    entity: EntityCandidate
    weight: float = Field(ge=0, le=1)
    source: Literal["manual", "broker"]


class SeedResponse(StrictModel):
    seed: Seed
    created: bool


class SeedsStatusResponse(StrictModel):
    status: Literal["not_started", "building", "ready", "failed"]
    updated_at: datetime
    detail: str | None = None


class FeedCard(StrictModel):
    event_id: str = Field(pattern=r"^evt_[A-Za-z0-9_-]+$")
    revision: int = Field(ge=1)
    category: str
    title: str
    why: str
    score: int = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    hops: int = Field(ge=0, le=3)
    importance: float = Field(ge=0, le=1)
    published_at: datetime
    article_count: int = Field(ge=1)
    primary_source: str
    entities: list[EntityCandidate]
    path: list[str]
    slot_type: Literal["graph", "keyword_probe"]
    slot_position: int = Field(ge=1)


class FeedResponse(StrictModel):
    items: list[FeedCard]
    next_cursor: str | None = None


class EventArticle(StrictModel):
    id: str = Field(pattern=r"^art_[A-Za-z0-9_-]+$")
    publisher: str
    title: str
    url: str
    published_at: datetime


class EventDetailResponse(StrictModel):
    requested_event_id: str = Field(pattern=r"^evt_[A-Za-z0-9_-]+$")
    event: FeedCard
    articles: list[EventArticle]
    merged_from: list[str] = []


class GraphNode(StrictModel):
    id: str = Field(pattern=r"^ent_[A-Za-z0-9_-]+$")
    name: str
    entity_type: str


class GraphEdge(StrictModel):
    source_id: str
    target_id: str
    relation: str
    confidence: float = Field(ge=0, le=1)


class GraphResponse(StrictModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class EntityResponse(StrictModel):
    entity: EntityCandidate
    graph_status: Literal["catalog", "building", "live"]


class Profile(StrictModel):
    timezone: str
    notification_percentile: int = Field(ge=0, le=100)
    quiet_start: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    quiet_end: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


class ProfileResponse(StrictModel):
    profile: Profile


class ProfilePatchRequest(StrictModel):
    timezone: str | None = None
    notification_percentile: int | None = Field(default=None, ge=0, le=100)
    quiet_start: str | None = Field(default=None, pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    quiet_end: str | None = Field(default=None, pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


class DeviceCreateRequest(StrictModel):
    apns_token: str = Field(min_length=1)
    platform: Literal["ios"]


class DeviceResponse(StrictModel):
    id: str = Field(pattern=r"^dev_[A-Za-z0-9_-]+$")
    registered: bool


class FeedbackRequest(StrictModel):
    event_id: str = Field(pattern=r"^evt_[A-Za-z0-9_-]+$")
    action: Literal["impression", "click", "dismiss", "not_relevant", "save"]
    slot_type: Literal["graph", "keyword_probe"]
    slot_position: int = Field(ge=1)
    occurred_at: datetime
    idempotency_key: str = Field(min_length=8, max_length=128)


class FeedbackResponse(StrictModel):
    accepted: bool
    deduplicated: bool


class LoadingResponse(StrictModel):
    state: Literal["loading"]
    retry_after_seconds: int = Field(ge=0)


class AccountDeletionResponse(StrictModel):
    status: Literal["scheduled"]
