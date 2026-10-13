from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.discovery import (
    DiscoveryCandidate,
    DiscoveryConfigurationError,
    EntitySweep,
    FeedPoller,
    SearchRequest,
    SourceConfig,
    declared_user_agent,
    entity_sweep,
    load_sources,
)
from app.operations import SearchBudgetExceeded

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)
AGENT = "TeVarta/1.0 (ops@tevarta.test)"


def source(mode="rss", active=True):
    return SourceConfig(
        id="fixture_source",
        domain="example.invalid",
        feed_url="https://example.invalid/feed",
        publisher_group="fixture_publisher",
        tier_value=1.0,
        reliability=0.99,
        poll_interval_s=300,
        active=active,
        ingestion_mode=mode,
    )


RSS = (
    b"<rss><channel><item><title>Fixture headline</title>"
    b"<link>https://example.invalid/item</link><description>Metadata only</description>"
    b"<pubDate>Sat, 20 Sep 2026 10:00:00 +0000</pubDate></item></channel></rss>"
)


def test_feed_polling_emits_the_contract_candidate_shape_without_fetching_bodies():
    calls = []
    poller = FeedPoller(
        lambda url, headers: calls.append((url, headers)) or RSS, now=lambda: NOW,
        user_agent=AGENT,
    )

    candidates = poller.poll_active((source(),))

    assert candidates == (
        DiscoveryCandidate(
            url="https://example.invalid/item",
            title="Fixture headline",
            snippet="Metadata only",
            publisher="fixture_publisher",
            published_at=datetime(2026, 9, 20, 10, tzinfo=UTC),
            source_hint="fixture_source",
        ),
    )
    assert calls[0][0] == "https://example.invalid/feed"
    assert "application/rss+xml" in calls[0][1]["Accept"]
    assert calls[0][1]["User-Agent"] == AGENT


def test_inactive_sources_never_make_network_requests():
    poller = FeedPoller(lambda _url, _headers: pytest.fail("inactive feed was fetched"))
    assert poller.poll_active((source(active=False),)) == ()


def test_feed_failure_does_not_discard_candidates_from_other_sources():
    working = source()
    broken = SourceConfig(
        id="broken_source",
        domain="broken.example.invalid",
        feed_url="https://broken.example.invalid/feed",
        publisher_group="broken_publisher",
        tier_value=1.0,
        reliability=0.99,
        poll_interval_s=300,
        active=True,
        ingestion_mode="rss",
    )

    def fetcher(url, _headers):
        if url == broken.feed_url:
            raise OSError("fixture network failure")
        return RSS

    candidates = FeedPoller(fetcher, now=lambda: NOW, user_agent=AGENT).poll_active(
        (working, broken)
    )

    assert [candidate.source_hint for candidate in candidates] == [working.id]


def test_active_source_is_never_fetched_without_a_declared_user_agent():
    poller = FeedPoller(lambda _url, _headers: pytest.fail("undeclared client fetched"))

    with pytest.raises(DiscoveryConfigurationError, match="declared User-Agent"):
        poller.poll(source())
    assert poller.poll_active((source(),)) == ()


@pytest.mark.parametrize(
    "contact", ["", "TeVarta", "ops at tevarta.com", "@tevarta.com", "ops@localhost"]
)
def test_declared_user_agent_requires_an_operations_email(contact):
    with pytest.raises(DiscoveryConfigurationError, match="TEVARTA_HTTP_CONTACT"):
        declared_user_agent({"TEVARTA_HTTP_CONTACT": contact})


def test_declared_user_agent_names_the_client_and_contact():
    agent = declared_user_agent({"TEVARTA_HTTP_CONTACT": " ops@tevarta.com "})

    assert agent == "TeVarta/1.0 (ops@tevarta.com)"


def test_poll_task_without_active_sources_needs_no_contact(monkeypatch):
    from app.discovery import poll_sources

    monkeypatch.delenv("TEVARTA_HTTP_CONTACT", raising=False)

    assert poll_sources.run() == 0


def test_source_registry_keeps_every_source_disabled_until_t19_rights_review():
    sources = load_sources()
    assert len(sources) == 20
    assert all(not item.active for item in sources)
    assert all(item.poll_interval_s == 300 for item in sources)


def test_source_registry_rejects_non_mapping_entries(tmp_path):
    config = tmp_path / "sources.yaml"
    config.write_text("sources:\n  - not-a-source\n")
    with pytest.raises(DiscoveryConfigurationError, match="mapping"):
        load_sources(config)


class FakeSearch:
    def __init__(self):
        self.requests = []

    def search(self, request):
        self.requests.append(request)
        return (
            DiscoveryCandidate(
                "https://example.invalid/search",
                "Search result",
                "Snippet",
                "search",
                NOW,
                "search",
            ),
        )


class FakeBudget:
    def __init__(self):
        self.usage = []

    def reserve(self, request_id, now):
        from app.discovery import SearchReservation

        return SearchReservation(request_id, now.date(), 1, 1500, False, False)

    def record_usage(self, request_id, outcome, query_count):
        self.usage.append((request_id, outcome, query_count))


class FakeEntities:
    def top_seeded_entities(self, limit):
        assert limit == 50
        return (("ent_apple", "Apple Inc."), ("ent_sandisk", "SanDisk Corporation"))


def test_daily_entity_sweep_uses_top_seeded_entities_and_last_24_hours():
    from app.discovery import SearchService

    provider = FakeSearch()
    budget = FakeBudget()
    result = EntitySweep(FakeEntities(), SearchService(provider, budget), now=lambda: NOW).run()

    assert len(result) == 2
    assert [request.entity_ids for request in provider.requests] == [
        ("ent_apple",),
        ("ent_sandisk",),
    ]
    assert all(
        request.published_after == NOW - timedelta(hours=24) for request in provider.requests
    )
    assert all(request.max_results == 50 for request in provider.requests)
    assert [outcome for _, outcome, _ in budget.usage] == ["success", "success"]


def test_daily_entity_sweep_returns_already_found_candidates_when_budget_is_exhausted():
    from app.discovery import SearchReservation, SearchService

    class BudgetExhaustedAfterOne(FakeBudget):
        def __init__(self):
            super().__init__()
            self.reservations = 0

        def reserve(self, request_id, now):
            self.reservations += 1
            if self.reservations > 1:
                raise SearchBudgetExceeded("monthly search query cap reached")
            return SearchReservation(request_id, now.date(), 1, 1, False, False)

    provider = FakeSearch()
    budget = BudgetExhaustedAfterOne()
    result = EntitySweep(FakeEntities(), SearchService(provider, budget), now=lambda: NOW).run()

    assert len(result) == 1
    assert [request.entity_ids for request in provider.requests] == [("ent_apple",)]
    assert [outcome for _, outcome, _ in budget.usage] == ["success", "blocked"]


def test_search_request_requires_a_timezone_and_contract_result_limit():
    with pytest.raises(ValueError, match="timezone-aware"):
        SearchRequest(uuid4(), "Apple", datetime(2026, 9, 20), 10)
    with pytest.raises(ValueError, match="1–50"):
        SearchRequest(uuid4(), "Apple", NOW, 51)


def test_worker_schedule_has_five_minute_poll_and_daily_sweep():
    from app import worker_settings

    assert worker_settings.beat_schedule["poll-sources-every-five-minutes"]["schedule"] == 300.0
    assert worker_settings.beat_schedule["entity-sweep-daily"]["schedule"] == 86400.0
    assert entity_sweep.name == "app.discovery.entity_sweep"


def test_poll_task_with_active_sources_fails_closed_without_contact(monkeypatch, tmp_path):
    from app.discovery import poll_sources

    config = tmp_path / "sources.yaml"
    config.write_text(
        "sources:\n"
        "  - {id: fixture_source, domain: example.invalid, "
        "feed_url: 'https://example.invalid/feed', publisher_group: fixture, "
        "tier_value: 1.0, reliability: 0.99, poll_interval_s: 300, active: true, "
        "ingestion_mode: rss}\n"
    )
    monkeypatch.setenv("TEVARTA_SOURCES_CONFIG", str(config))
    monkeypatch.delenv("TEVARTA_HTTP_CONTACT", raising=False)

    with pytest.raises(DiscoveryConfigurationError, match="TEVARTA_HTTP_CONTACT"):
        poll_sources.run()
