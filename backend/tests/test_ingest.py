"""T20 metadata stage without a database: URL identity, mentions, tagging, tiers."""

import pytest
from app.ingest import (
    AliasHit,
    IngestInputError,
    MetadataTagger,
    assign_tier,
    clean_text,
    extract_mentions,
    normalize_url,
    url_hash,
)


class FakeIndex:
    """alias_norm → hits, mirroring entity_aliases JOIN entities."""

    def __init__(self, aliases):
        self.aliases = aliases
        self.queries = []

    def lookup(self, keys):
        self.queries.append(set(keys))
        return {key: self.aliases[key] for key in keys if key in self.aliases}


def hit(entity_id, status="catalog", seed_count=0, identifier=False):
    return AliasHit(entity_id, entity_id, seed_count, status, identifier)


CATALOG = {
    "apple": [hit("ent_apple_inc", "live", 5), hit("ent_apple_fruit", "catalog", 0)],
    "ticker:aapl": [hit("ent_apple_inc", "live", 5, True)],
    "cik:0000320193": [hit("ent_apple_inc", "live", 5, True)],
    "iphone": [hit("ent_iphone", "catalog")],
    "taiwan": [hit("ent_cur_taiwan", "live")],
    "taiwansemiconductormanufacturing": [hit("ent_tsmc", "live", 3)],
    "tsmc": [hit("ent_tsmc", "live", 3)],
    "target": [hit("ent_target_corp", "catalog")],
    "nasdaq": [hit("ent_nasdaq_inc", "catalog")],
    "sandisk": [hit("ent_sandisk", "catalog")],
    "bankofamerica": [hit("ent_bac", "catalog")],
    "mercury": [hit("ent_mercury_systems", "catalog", 2), hit("ent_mercury_planet", "catalog", 2)],
}


def tag(title, snippet=None, domain="example.org"):
    return MetadataTagger(FakeIndex(CATALOG)).tag(title, snippet, domain)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://www.Reuters.com/tech/apple/?utm_source=x&utm_medium=y",
         "https://reuters.com/tech/apple"),
        ("http://example.org/a?b=2&a=1#section", "https://example.org/a?a=1&b=2"),
        ("https://example.org:443//news//item/", "https://example.org/news/item"),
        ("https://example.org/a?fbclid=1&gclid=2&id=7", "https://example.org/a?id=7"),
    ],
)
def test_equivalent_urls_share_one_dedupe_key(left, right):
    assert normalize_url(left) == normalize_url(right)
    assert url_hash(left) == url_hash(right)


def test_distinct_documents_keep_distinct_keys():
    assert url_hash("https://example.org/a?id=1") != url_hash("https://example.org/a?id=2")
    assert url_hash("https://example.org/News") != url_hash("https://example.org/news")


@pytest.mark.parametrize("url", ["ftp://example.org/a", "/relative/path", "https:///nohost"])
def test_non_http_urls_are_rejected(url):
    with pytest.raises(IngestInputError):
        normalize_url(url)


def test_feed_html_is_reduced_to_plain_text():
    assert clean_text("<b>Filed:</b> 2026-10-13 &amp; <i>AccNo:</i>") == (
        "Filed: 2026-10-13 & AccNo:"
    )
    assert clean_text("&lt;p&gt;Escaped&lt;/p&gt; tags") == "Escaped tags"
    assert clean_text("   ") is None


def test_live_country_makes_an_upstream_event_tier_one():
    # §7.3 example 1: no company is named, yet Taiwan is live once TSMC is seeded.
    result = tag("M 7.1 - 25 km E of Hualien City, Taiwan")
    assert [entity.entity_id for entity in result.entities] == ["ent_cur_taiwan"]
    assert result.tier == 1


def test_catalog_only_match_is_tier_two_and_no_match_is_tier_three():
    assert tag("SanDisk expands flash output").tier == 2
    assert tag("Markets drift ahead of holiday weekend").tier == 3


def test_longest_name_wins_over_its_parts():
    result = tag("Taiwan Semiconductor Manufacturing raises capex")
    assert [entity.entity_id for entity in result.entities] == ["ent_tsmc"]


def test_lowercase_common_words_are_not_company_names():
    assert tag("Fed says it will keep its inflation target").entities == ()


def test_identifier_syntax_resolves_and_is_not_also_read_as_a_name():
    result = tag("NASDAQ: AAPL slides after guidance")
    assert [entity.entity_id for entity in result.entities] == ["ent_apple_inc"]


def test_sec_filer_cik_in_parentheses_resolves_only_for_sec_sources():
    title = "8-K - SOMETHING HOLDINGS (0000320193) (Filer)"
    assert [e.entity_id for e in tag(title, domain="sec.gov").entities] == ["ent_apple_inc"]
    assert tag(title, domain="example.org").entities == ()


def test_ambiguous_alias_uses_cooccurrence_then_refuses_to_guess():
    # "Apple" alone is two candidates; the ticker in the snippet settles it (§6.4a).
    settled = tag("Apple shares rise", "Investors bought $AAPL after the event.")
    assert [e.entity_id for e in settled.entities] == ["ent_apple_inc"]
    # Equal seed counts and no co-occurrence: tag none and report it (§6.4).
    refused = tag("Mercury rising")
    assert refused.entities == ()
    assert refused.ambiguous[0].candidates == ("ent_mercury_planet", "ent_mercury_systems")


def test_seed_count_breaks_an_alias_tie_without_context():
    result = tag("Apple unveils new devices")
    assert [e.entity_id for e in result.entities] == ["ent_apple_inc"]


def test_connector_words_join_names_but_never_start_or_end_them():
    assert [e.entity_id for e in tag("Bank of America cuts rates").entities] == ["ent_bac"]
    assert tag("Shares of the bank fell").entities == ()


def test_title_mentions_are_more_salient_than_snippet_mentions():
    result = tag("SanDisk expands output", "Supply talks continue in Taiwan.")
    by_id = {entity.entity_id: entity for entity in result.entities}
    assert by_id["ent_sandisk"].salience == 1.0 and by_id["ent_sandisk"].in_title
    assert by_id["ent_cur_taiwan"].salience == 0.6 and not by_id["ent_cur_taiwan"].in_title


def test_punctuation_breaks_names():
    # "Apple, Taiwan" must not be read as one name "apple taiwan".
    mentions = extract_mentions("Apple, Taiwan", None, "example.org")
    assert "appletaiwan" not in {mention.key for mention in mentions}


def test_tagging_issues_one_batched_lookup():
    index = FakeIndex(CATALOG)
    MetadataTagger(index).tag("Apple and TSMC in Taiwan", "Bank of America notes", "x.org")
    assert len(index.queries) == 1


def test_tier_gate_covers_every_status_mix():
    assert assign_tier([]) == 3
    assert assign_tier(["catalog"]) == 2
    assert assign_tier(["catalog", "live"]) == 1
