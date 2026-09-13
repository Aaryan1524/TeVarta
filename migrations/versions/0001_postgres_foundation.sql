-- Immutable SQL for revision 0001. Future changes belong in new revisions.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    public_id TEXT NOT NULL UNIQUE CHECK (public_id ~ '^usr_[A-Za-z0-9_-]+$'),
    apple_sub TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    tz TEXT NOT NULL DEFAULT 'UTC',
    notif_threshold NUMERIC NOT NULL DEFAULT 97 CHECK (notif_threshold BETWEEN 0 AND 100),
    quiet_start TIME NOT NULL DEFAULT '22:00',
    quiet_end TIME NOT NULL DEFAULT '07:00'
);
CREATE TABLE devices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    public_id TEXT NOT NULL UNIQUE CHECK (public_id ~ '^dev_[A-Za-z0-9_-]+$'),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    apns_token_enc BYTEA NOT NULL CHECK (octet_length(apns_token_enc) > 0),
    platform TEXT NOT NULL CHECK (platform = 'ios'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_devices_user ON devices(user_id);

CREATE TABLE entities (
    entity_id TEXT PRIMARY KEY CHECK (entity_id <> ''),
    canonical_name TEXT NOT NULL CHECK (canonical_name <> ''),
    type TEXT NOT NULL CHECK (type IN ('Company', 'Security', 'Product', 'Facility',
        'Commodity', 'Industry', 'Country', 'Person', 'Regulation', 'MacroFactor',
        'Topic', 'Organization')),
    ticker TEXT,
    cik TEXT,
    status TEXT NOT NULL DEFAULT 'catalog' CHECK (status IN ('catalog', 'live')),
    seed_count INTEGER NOT NULL DEFAULT 0 CHECK (seed_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE user_seeds (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    weight NUMERIC NOT NULL DEFAULT 1.0 CHECK (weight BETWEEN 0 AND 1),
    source TEXT NOT NULL CHECK (source IN ('onboarding', 'manual', 'broker')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, entity_id)
);
CREATE INDEX ix_user_seeds_entity ON user_seeds(entity_id);
CREATE TABLE entity_aliases (
    alias_norm TEXT NOT NULL CHECK (alias_norm <> ''),
    entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    confidence NUMERIC NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    is_identifier BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (alias_norm, entity_id)
);
CREATE INDEX ix_entity_aliases_alias ON entity_aliases(alias_norm);
CREATE UNIQUE INDEX uq_entity_alias_identifier ON entity_aliases(alias_norm) WHERE is_identifier;

CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    feed_url TEXT,
    publisher_group TEXT NOT NULL CHECK (publisher_group <> ''),
    tier_value NUMERIC NOT NULL CHECK (tier_value IN (1.0, 0.9, 0.7, 0.5, 0.25)),
    reliability NUMERIC NOT NULL CHECK (reliability BETWEEN 0 AND 1),
    poll_interval_s INTEGER NOT NULL CHECK (poll_interval_s > 0),
    active BOOLEAN NOT NULL DEFAULT false,
    CHECK (NOT active OR (feed_url IS NOT NULL AND feed_url <> ''))
);
CREATE TABLE articles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    public_id TEXT NOT NULL UNIQUE CHECK (public_id ~ '^art_[A-Za-z0-9_-]+$'),
    url_hash TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(id),
    title TEXT NOT NULL,
    snippet TEXT,
    published_at TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ,
    simhash BIGINT,
    r2_key TEXT,
    lang TEXT,
    tier INTEGER CHECK (tier BETWEEN 1 AND 3),
    embedding VECTOR(1024),
    embedding_scope TEXT CHECK (embedding_scope IN ('body', 'title_snippet')),
    CHECK ((embedding IS NULL) = (embedding_scope IS NULL))
);
CREATE INDEX ix_articles_source ON articles(source_id);
CREATE INDEX ix_articles_published ON articles(published_at);
CREATE TABLE article_entities (
    article_id UUID NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    mentions INTEGER NOT NULL CHECK (mentions > 0),
    salience NUMERIC NOT NULL CHECK (salience BETWEEN 0 AND 1),
    found_in TEXT NOT NULL CHECK (found_in IN ('metadata', 'body')),
    PRIMARY KEY (article_id, entity_id)
);
CREATE INDEX ix_article_entities_entity ON article_entities(entity_id);
CREATE TABLE quarantine (
    article_id UUID PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    tagged_entity_ids TEXT[] NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX ix_quarantine_expires ON quarantine(expires_at);
CREATE INDEX ix_quarantine_entities ON quarantine USING gin(tagged_entity_ids);
CREATE TABLE recall_audit (
    article_id UUID PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    tier_assigned INTEGER NOT NULL CHECK (tier_assigned BETWEEN 1 AND 3),
    entities_found_in_body TEXT[] NOT NULL,
    ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    public_id TEXT NOT NULL UNIQUE CHECK (public_id ~ '^evt_[A-Za-z0-9_-]+$'),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    canonical_title TEXT NOT NULL,
    event_type TEXT,
    severity_class TEXT CHECK (severity_class IN ('critical', 'high', 'moderate', 'low', 'routine')),
    severity NUMERIC CHECK (severity BETWEEN 0 AND 1),
    importance NUMERIC CHECK (importance BETWEEN 0 AND 1),
    confidence NUMERIC CHECK (confidence BETWEEN 0 AND 1),
    status TEXT NOT NULL DEFAULT 'forming' CHECK (status IN ('forming', 'active', 'merged')),
    merged_into UUID REFERENCES events(id),
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_update TIMESTAMPTZ NOT NULL DEFAULT now(),
    article_count INTEGER NOT NULL DEFAULT 0 CHECK (article_count >= 0),
    centroid VECTOR(1024),
    CHECK (merged_into IS NULL OR merged_into <> id),
    CHECK ((status = 'merged') = (merged_into IS NOT NULL)),
    CHECK (last_update >= first_seen),
    CHECK ((severity_class IS NULL) = (severity IS NULL)),
    CHECK (severity = CASE severity_class WHEN 'critical' THEN 1.0 WHEN 'high' THEN 0.8
        WHEN 'moderate' THEN 0.55 WHEN 'low' THEN 0.3 WHEN 'routine' THEN 0.15 END)
);
CREATE INDEX ix_events_recent ON events(last_update);
CREATE INDEX ix_events_merged_into ON events(merged_into) WHERE merged_into IS NOT NULL;
CREATE TABLE event_articles (
    event_id UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    article_id UUID NOT NULL REFERENCES articles(id),
    PRIMARY KEY (event_id, article_id)
);
CREATE INDEX ix_event_articles_article ON event_articles(article_id);
CREATE TABLE event_entities (
    event_id UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    role TEXT NOT NULL,
    PRIMARY KEY (event_id, entity_id)
);
CREATE INDEX ix_event_entities_entity ON event_entities(entity_id);
CREATE TABLE event_summaries (
    event_id UUID PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    summary TEXT NOT NULL,
    model TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE why_cache (
    event_id UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    path_signature TEXT NOT NULL,
    text TEXT NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('template', 'llm')),
    PRIMARY KEY (event_id, path_signature)
);

-- Validate each logged measurement, not just the presence of seven keys.
CREATE FUNCTION tevarta_valid_components(value JSONB) RETURNS BOOLEAN
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT jsonb_typeof(value) = 'object' AND NOT EXISTS (
        SELECT 1 FROM unnest(ARRAY['path_score', 'importance', 'freshness',
            'portfolio_exposure', 'source_reliability', 'novelty', 'explicit_interest']) AS k
        WHERE jsonb_typeof(value -> k) IS DISTINCT FROM 'number'
    );
$$;
CREATE TABLE user_feed (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id UUID NOT NULL REFERENCES events(id),
    score NUMERIC NOT NULL CHECK (score BETWEEN 0 AND 100),
    components JSONB NOT NULL CHECK (tevarta_valid_components(components)),
    path_json JSONB NOT NULL CHECK (jsonb_typeof(path_json) IN ('object', 'array')),
    shadow_score NUMERIC NOT NULL CHECK (shadow_score NOT IN ('NaN', 'Infinity', '-Infinity')),
    shadow_rank INTEGER NOT NULL CHECK (shadow_rank > 0),
    slot_type TEXT NOT NULL CHECK (slot_type IN ('graph', 'keyword_probe')),
    slot_position INTEGER NOT NULL CHECK (slot_position > 0),
    channel TEXT NOT NULL CHECK (channel IN ('feed', 'push', 'digest', 'logged')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, event_id)
);
CREATE INDEX ix_user_feed_rank ON user_feed(user_id, score DESC, created_at DESC);
CREATE INDEX ix_user_feed_event ON user_feed(event_id);
CREATE TABLE score_distribution (
    day DATE PRIMARY KEY,
    p50 NUMERIC NOT NULL CHECK (p50 BETWEEN 0 AND 100),
    p75 NUMERIC NOT NULL CHECK (p75 BETWEEN p50 AND 100),
    p95 NUMERIC NOT NULL CHECK (p95 BETWEEN p75 AND 100),
    p97 NUMERIC NOT NULL CHECK (p97 BETWEEN p95 AND 100),
    p99 NUMERIC NOT NULL CHECK (p99 BETWEEN p97 AND 100)
);
CREATE TABLE notification_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id UUID NOT NULL REFERENCES events(id),
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_notification_log_user_sent ON notification_log(user_id, sent_at DESC);
CREATE TABLE feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id UUID REFERENCES events(id),
    action TEXT NOT NULL CHECK (action IN ('app_open', 'feed_view', 'card_impression',
        'card_click', 'card_dismiss', 'not_relevant', 'save', 'push_received', 'push_opened',
        'digest_opened', 'seed_added', 'seed_removed', 'session_length',
        'impression', 'click', 'dismiss')),
    slot_type TEXT CHECK (slot_type IN ('graph', 'keyword_probe')),
    slot_position INTEGER CHECK (slot_position > 0),
    session_duration_s NUMERIC CHECK (
        session_duration_s >= 0 AND session_duration_s NOT IN ('NaN', 'Infinity')),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 128),
    ts TIMESTAMPTZ NOT NULL,
    UNIQUE (user_id, idempotency_key),
    CHECK ((slot_type IS NULL) = (slot_position IS NULL)),
    CHECK (action NOT IN ('card_impression', 'card_click', 'card_dismiss',
        'not_relevant', 'save', 'impression', 'click', 'dismiss')
        OR (event_id IS NOT NULL AND slot_type IS NOT NULL)),
    CHECK (action <> 'session_length' OR session_duration_s IS NOT NULL)
);
CREATE INDEX ix_feedback_event ON feedback(event_id);
CREATE INDEX ix_feedback_ts ON feedback(ts);
CREATE TABLE resolution_misses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    surface_form TEXT NOT NULL,
    context TEXT NOT NULL,
    article_id UUID REFERENCES articles(id) ON DELETE SET NULL,
    ts TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE demand_log (
    entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    quarantined_article_count INTEGER NOT NULL CHECK (quarantined_article_count >= 0),
    week DATE NOT NULL,
    PRIMARY KEY (entity_id, week)
);

-- Relational claim provenance; provider bodies stay in the private evidence store.
CREATE TABLE event_claims (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    event_revision INTEGER NOT NULL CHECK (event_revision > 0),
    article_id UUID NOT NULL REFERENCES articles(id),
    text TEXT NOT NULL CHECK (text <> ''),
    directness TEXT NOT NULL CHECK (directness IN ('stated', 'inferred')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_event_claims_revision ON event_claims(event_id, event_revision);
CREATE TABLE usage_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id UUID NOT NULL,
    component TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failure', 'blocked')),
    quantity BIGINT NOT NULL CHECK (quantity >= 0),
    unit TEXT NOT NULL,
    estimated_cost_usd NUMERIC NOT NULL CHECK (
        estimated_cost_usd >= 0 AND estimated_cost_usd NOT IN ('NaN', 'Infinity')),
    provider TEXT,
    model TEXT,
    input_tokens BIGINT CHECK (input_tokens >= 0),
    output_tokens BIGINT CHECK (output_tokens >= 0),
    query_count BIGINT CHECK (query_count >= 0),
    latency_ms BIGINT CHECK (latency_ms >= 0),
    UNIQUE (request_id, component)
);
CREATE INDEX ix_usage_component_time ON usage_records(component, occurred_at);
