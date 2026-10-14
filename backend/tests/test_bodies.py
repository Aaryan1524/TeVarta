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
DETAIL = " Supply was tight across the region as plants restarted." * 8  # > MIN_BODY_CHARS


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
        200, "text/html", f"<p>Fab output fell.{DETAIL}</p>".encode())})
    assert body_fetcher.fetch("https://news.example.org/a") == "Fab output fell." + DETAIL
    assert web.requests[-1][1]["User-Agent"] == "TeVarta/1.0 (ops@tevarta.com)"


SCRIPT_SHELL = (b"<html><body><p>The Earthquake Event Page application supports most recent "
                b"browsers, view supported browsers.</p><script>app()</script></body></html>")


@pytest.mark.parametrize("page", [(404, "text/html", b""), (200, "application/pdf", b"%PDF"),
                                  (200, "text/html", b"<script>x()</script>"),
                                  (200, "text/html", SCRIPT_SHELL)])
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


FILING = "/Archives/edgar/data/1701756/000173112226001341/"


def sec_index(form, primary, exhibit_type, exhibit):
    """A filing index table shaped like EDGAR's (Seq, Description, Document, Type, Size)."""
    def row(link, kind):
        return (f'<tr><td scope="row">1</td><td scope="row">doc</td>'
                f'<td scope="row"><a href="{link}">x</a></td>'
                f'<td scope="row">{kind}</td><td scope="row">1</td></tr>')
    return ("<table><tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th></tr>"
            + row(f"/ix?doc={FILING}{primary}", form) + row(FILING + exhibit, exhibit_type)
            + row(FILING + "submission.txt", "&nbsp;") + "</table>")


SEC_8K_INDEX = sec_index("8-K", "e8005_8-k.htm", "EX-10.1", "e8005_ex10-1.htm")
SEC_6K_INDEX = sec_index("6-K", "form6-k.htm", "EX-99.1", "ex99-1.htm")
INDEX_URL = f"https://www.sec.gov{FILING}0001731122-26-001341-index.htm"


def test_sec_index_yields_the_primary_document_then_press_exhibits():
    from app.bodies import sec_filing_documents

    assert sec_filing_documents(SEC_8K_INDEX) == [f"https://www.sec.gov{FILING}e8005_8-k.htm"]
    assert sec_filing_documents(SEC_6K_INDEX) == [
        f"https://www.sec.gov{FILING}form6-k.htm", f"https://www.sec.gov{FILING}ex99-1.htm"]


def sec_fetcher(pages, robots_body=b""):
    web = FakeWeb({"https://www.sec.gov/robots.txt": (200, "text/plain", robots_body), **pages})
    agent = "TeVarta/1.0 (ops@tevarta.com)"
    return BodyFetcher(web, RobotsPolicy(web, agent, now=lambda: NOW), agent), web


def test_an_sec_filing_body_is_its_documents_not_its_index_page():
    base = f"https://www.sec.gov{FILING}"
    body_fetcher, web = sec_fetcher({
        INDEX_URL: (200, "text/html", SEC_6K_INDEX.encode()),
        base + "form6-k.htm": (200, "text/html", b"<p>Form 6-K cover.</p>"),
        base + "ex99-1.htm": (200, "text/html",
                              f"<p>TSMC reports record revenue.{DETAIL}</p>".encode()),
    })
    expected = "Form 6-K cover.\n\nTSMC reports record revenue." + DETAIL
    assert body_fetcher.fetch(INDEX_URL) == expected


def test_an_sec_index_without_readable_documents_falls_back_to_the_index_text():
    body_fetcher, _ = sec_fetcher({INDEX_URL: (200, "text/html",
                                               f"<p>Filing index{DETAIL}</p>".encode())})
    assert body_fetcher.fetch(INDEX_URL) == "Filing index" + DETAIL


def test_sec_documents_disallowed_by_robots_are_skipped():
    body_fetcher, web = sec_fetcher(
        {INDEX_URL: (200, "text/html", SEC_8K_INDEX.encode())},
        f"User-agent: *\nDisallow: {FILING}e8005\n".encode())
    with pytest.raises(BodyFetchRefused, match="no usable text"):
        body_fetcher.fetch(INDEX_URL)  # the index table alone is not a body
    assert "e8005_8-k.htm" not in [url.rsplit("/", 1)[-1] for url, _ in web.requests]


def test_title_similarity_ignores_wire_prefixes_and_separates_documents():
    from app.bodies import TITLE_MATCH_MIN, title_similarity

    story = "TSMC raises prices for advanced chips"
    assert title_similarity(story, "UPDATE 2-TSMC raises prices for advanced chips") == 1.0
    assert title_similarity(story, "TSMC raises prices on advanced chips, sources say") >= (
        TITLE_MATCH_MIN)
    assert title_similarity("Self-Regulatory Organizations; Cboe BYX Exchange",
                            "Joint Industry Plan; Notice of Filing") < TITLE_MATCH_MIN


def test_federal_register_documents_are_read_from_their_full_text_path():
    from app.bodies import federal_register_text_url

    page = ("https://www.federalregister.gov/documents/2026/10/13/2026-20969/"
            "national-energy-dominance-month-2026")
    text_url = "https://www.federalregister.gov/documents/full_text/text/2026/10/13/2026-20969.txt"
    assert federal_register_text_url(page) == text_url
    assert federal_register_text_url("https://news.example.org/documents/a") is None

    # The real full-text response is HTML-wrapped plain text, served as text/plain.
    raw = f"<html><head><title>FR</title></head><body><pre>Proclamation text.{DETAIL}</pre>"
    web = FakeWeb({"https://www.federalregister.gov/robots.txt": (200, "text/plain", b""),
                   text_url: (200, "text/plain", raw.encode())})
    agent = "TeVarta/1.0 (ops@tevarta.com)"
    body = BodyFetcher(web, RobotsPolicy(web, agent, now=lambda: NOW), agent).fetch(page)
    assert body.startswith("Proclamation text.") and page not in [u for u, _ in web.requests]


class RedirectingWeb(FakeWeb):
    """FakeWeb whose entries may be Redirect(location), as http_get raises on a 3xx."""

    def __call__(self, url, headers):
        self.requests.append((url, dict(headers)))
        page = self.pages.get(url, (404, "text/plain", b""))
        if isinstance(page, Exception):
            raise page
        return page


def redirecting_fetcher(pages, allow_url=None):
    web = RedirectingWeb(pages)
    agent = "TeVarta/1.0 (ops@tevarta.com)"
    robots = RobotsPolicy(web, agent, now=lambda: NOW, allow_url=allow_url)
    return BodyFetcher(web, robots, agent, allow_url=allow_url), web


def test_redirects_are_followed_with_robots_checked_on_every_hop():
    from app.bodies import Redirect

    ok = (200, "text/plain", b"")
    page = (200, "text/html", f"<p>Moved story.{DETAIL}</p>".encode())
    body_fetcher, web = redirecting_fetcher({
        "https://news.example.org/robots.txt": ok, "https://cdn.example.net/robots.txt": ok,
        "https://news.example.org/a": Redirect("https://cdn.example.net/story"),
        "https://cdn.example.net/story": page})
    assert body_fetcher.fetch("https://news.example.org/a").startswith("Moved story.")
    assert "https://cdn.example.net/robots.txt" in [url for url, _ in web.requests]


def test_a_redirect_to_a_disallowed_host_is_never_requested():
    from app.bodies import Redirect

    body_fetcher, web = redirecting_fetcher({
        "https://news.example.org/robots.txt": (200, "text/plain", b""),
        "https://news.example.org/a": Redirect("http://169.254.169.254/latest/meta-data/")},
        allow_url=lambda url: "169.254" not in url)
    with pytest.raises(BodyFetchRefused, match="public"):
        body_fetcher.fetch("https://news.example.org/a")
    assert all("169.254" not in url for url, _ in web.requests)


def test_redirect_loops_stop():
    from app.bodies import Redirect

    body_fetcher, _ = redirecting_fetcher({
        "https://news.example.org/robots.txt": (200, "text/plain", b""),
        "https://news.example.org/a": Redirect("https://news.example.org/a")})
    with pytest.raises(BodyFetchRefused, match="too many redirects"):
        body_fetcher.fetch("https://news.example.org/a")


@pytest.mark.parametrize(("url", "public"), [
    ("http://127.0.0.1/x", False), ("http://169.254.169.254/latest", False),
    ("http://10.0.0.5/x", False), ("http://[::1]/x", False), ("ftp://example.org/x", False),
    ("file:///etc/passwd", False), ("http://localhost/x", False)])
def test_non_public_destinations_are_rejected(url, public):
    from app.bodies import is_public_url

    assert is_public_url(url) is public
