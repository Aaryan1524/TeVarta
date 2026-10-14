"""T21 body stage pieces without services: SimHash, extraction, robots, embeddings."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from app.bodies import (
    EMBEDDING_DIMENSIONS,
    SIMHASH_MAX_DISTANCE,
    BodyFetcher,
    BodyFetchRefused,
    EmbeddingNotConfigured,
    LocalBodyStore,
    OpenAIEmbedder,
    RobotsPolicy,
    embedder_from_env,
    extract_text,
    hamming,
    simhash,
)

STORY = (
    "Taiwan Semiconductor Manufacturing said on Tuesday it would raise prices for its most "
    "advanced chips next year, citing higher costs for new fabs in Arizona and Japan. The "
    "world's largest contract chipmaker told customers that wafers made on its newest process "
    "would cost between three and five percent more, according to people familiar with the "
    "matter who asked not to be named because the talks are private. Demand for artificial "
    "intelligence accelerators has kept its leading-edge lines fully booked through the "
    "second half, while older nodes have seen weaker orders from smartphone and personal "
    "computer makers. Analysts said most large customers were likely to accept the increase "
    "because few alternatives can match the company's yields at scale. Shares rose two "
    "percent in Taipei trading after the report, outpacing the broader market index, and "
    "suppliers of packaging equipment also gained on expectations of further capacity "
    "spending. The company is due to report quarterly results next week and is expected to "
    "update its capital expenditure guidance for the year."
)
UNRELATED = (
    "The central bank held interest rates steady on Wednesday and signalled patience, saying "
    "inflation had cooled across services and goods but remained above its target. Officials "
    "noted that hiring had slowed in recent months while wage growth moderated, and that "
    "household spending stayed resilient despite tighter credit conditions. Several members "
    "argued for waiting until more data arrived before considering cuts, while two dissented "
    "in favour of an immediate reduction. Bond yields fell after the statement and the "
    "currency weakened against major peers as traders priced in easing early next year."
)


def test_simhash_is_a_signed_64_bit_fingerprint_that_ignores_syndication_edits():
    fingerprint = simhash(STORY)
    copies = [STORY + " Reporting by Jane Doe; Editing by John Roe.", "UPDATE 1-" + STORY,
              STORY.replace(" the ", " a ", 3), STORY[: int(len(STORY) * 0.95)]]

    assert -(2 ** 63) <= fingerprint < 2 ** 63
    assert simhash(STORY) == fingerprint
    assert all(hamming(fingerprint, simhash(copy)) <= SIMHASH_MAX_DISTANCE for copy in copies)
    assert hamming(fingerprint, simhash(UNRELATED)) > SIMHASH_MAX_DISTANCE


def test_extraction_keeps_article_text_and_drops_page_furniture():
    page = (b"<html><head><style>.x{}</style><script>track()</script></head><body>"
            b"<nav>Home | Markets</nav><h1>Fab fire</h1><p>A fire hit the fab&nbsp;today.</p>"
            b"<footer>Cookie policy</footer></body></html>")
    text = extract_text(page, "text/html; charset=utf-8")
    assert text == "Fab fire\nA fire hit the fab today."


class FakeWeb:
    def __init__(self, pages):
        self.pages, self.requests = pages, []

    def __call__(self, url, headers):
        self.requests.append((url, dict(headers)))
        return self.pages.get(url, (404, "text/plain", b""))


NOW = datetime(2026, 10, 13, tzinfo=UTC)


def policy(robots_status, robots_body=b"", clock=lambda: NOW):
    web = FakeWeb({"https://news.example.org/robots.txt": (robots_status, "text/plain",
                                                           robots_body)})
    return RobotsPolicy(web, "TeVarta/1.0 (ops@tevarta.com)", now=clock), web


def test_robots_rules_are_applied_to_our_user_agent():
    robots, _ = policy(200, b"User-agent: *\nDisallow: /private/\n")
    assert robots.allowed("https://news.example.org/story")
    assert not robots.allowed("https://news.example.org/private/story")


@pytest.mark.parametrize(("status", "allowed"), [(404, True), (403, False), (401, False),
                                                 (503, False)])
def test_missing_robots_allows_but_forbidden_or_unavailable_does_not(status, allowed):
    robots, _ = policy(status)
    assert robots.allowed("https://news.example.org/story") is allowed


def test_robots_is_fetched_once_per_host_per_day():
    clock = [NOW]
    robots, web = policy(200, b"", clock=lambda: clock[0])
    robots.allowed("https://news.example.org/a")
    robots.allowed("https://news.example.org/b")
    assert len(web.requests) == 1
    clock[0] = NOW + timedelta(days=2)
    robots.allowed("https://news.example.org/c")
    assert len(web.requests) == 2


def fetcher(pages, robots_body=b""):
    web = FakeWeb({"https://news.example.org/robots.txt": (200, "text/plain", robots_body),
                   **pages})
    agent = "TeVarta/1.0 (ops@tevarta.com)"
    return BodyFetcher(web, RobotsPolicy(web, agent, now=lambda: NOW), agent), web


def test_fetch_sends_the_declared_agent_and_returns_text():
    body_fetcher, web = fetcher({"https://news.example.org/a": (
        200, "text/html", b"<p>Fab output fell.</p>")})
    assert body_fetcher.fetch("https://news.example.org/a") == "Fab output fell."
    assert web.requests[-1][1]["User-Agent"] == "TeVarta/1.0 (ops@tevarta.com)"


@pytest.mark.parametrize("page", [(404, "text/html", b""), (200, "application/pdf", b"%PDF"),
                                  (200, "text/html", b"<script>x()</script>")])
def test_unusable_responses_are_refused(page):
    body_fetcher, _ = fetcher({"https://news.example.org/a": page})
    with pytest.raises(BodyFetchRefused):
        body_fetcher.fetch("https://news.example.org/a")


def test_disallowed_urls_are_never_requested():
    body_fetcher, web = fetcher({}, b"User-agent: *\nDisallow: /\n")
    with pytest.raises(BodyFetchRefused, match="robots"):
        body_fetcher.fetch("https://news.example.org/a")
    assert [url for url, _ in web.requests] == ["https://news.example.org/robots.txt"]


def test_local_store_is_content_addressed(tmp_path):
    store = LocalBodyStore(tmp_path)
    key, size = store.put("body text")
    assert store.put("body text") == (key, size)
    assert key.startswith("bodies/") and (tmp_path / key).read_text() == "body text"


def test_openai_embedder_requests_1024_dimensions_and_reports_tokens():
    sent = {}

    def post(url, body, headers):
        sent.update(url=url, body=json.loads(body), headers=headers)
        return json.dumps({"data": [{"embedding": [0.5] * EMBEDDING_DIMENSIONS}],
                           "usage": {"prompt_tokens": 42}}).encode()

    result = OpenAIEmbedder("sk-test", "text-embedding-3-small", 0.02, post=post).embed("hi")

    assert sent["body"] == {"model": "text-embedding-3-small", "input": "hi", "dimensions": 1024}
    assert sent["headers"]["Authorization"] == "Bearer sk-test"
    assert (len(result.vector), result.input_tokens) == (1024, 42)


def test_an_embedding_of_the_wrong_size_is_rejected():
    def post(url, body, headers):
        return json.dumps({"data": [{"embedding": [0.1] * 1536}], "usage": {}}).encode()

    with pytest.raises(ValueError, match="1536"):
        OpenAIEmbedder("sk-test", "m", 0.02, post=post).embed("hi")


@pytest.mark.parametrize("environ", [{}, {"OPENAI_API_KEY": "sk"},
                                     {"TEVARTA_EMBEDDING_USD_PER_MTOK": "0.02"}])
def test_embeddings_fail_closed_without_a_key_and_a_price(environ):
    with pytest.raises(EmbeddingNotConfigured):
        embedder_from_env(environ)


def test_configured_embedder_defaults_to_the_recorded_model():
    embedder = embedder_from_env({"OPENAI_API_KEY": "sk", "TEVARTA_EMBEDDING_USD_PER_MTOK": "0.02"})
    assert (embedder.model, embedder.usd_per_mtok) == ("text-embedding-3-small", 0.02)
