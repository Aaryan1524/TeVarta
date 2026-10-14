# T19 — Source validation (2026-10-14)

**Owner:** Aaryan (T19 crossover)
**MVP:** §7.1 push backbone, §7.4 content rights, §20 legal

`scripts/validate_sources.py` fetches each candidate feed once with the declared
User-Agent, parses it with the production poller, and checks robots.txt for the feed
and for one sample article. It reports evidence only; activation (`active: true`) is a
founder decision. Run it again before activating anything.

## Result

| Source | tier | Feed | robots (feed / article) | Recommendation |
|---|---:|---|---|---|
| sec_current_filings (8-K only) | 1.0 | ok, 40 | allow / allow | activate |
| sec_current_6k (new) | 1.0 | ok, 40 | allow / allow | activate |
| federal_register_documents | 1.0 | ok, 100 | allow / allow | activate |
| federal_reserve_press | 1.0 | ok, 20 | allow / allow | activate |
| cftc_press_releases (new URL) | 1.0 | ok, 10 | allow / allow | activate |
| doj_press_releases | 1.0 | ok, 50 | allow / allow | activate |
| eia_today_in_energy | 1.0 | ok, 15 | allow / allow | activate |
| usgs_earthquakes | 1.0 | ok (hourly window) | allow / allow | activate |
| ecb_press (replaces market information) | 1.0 | ok, press releases | allow / allow | review terms, then activate |
| bank_of_england_news | 1.0 | ok, 50 | allow / allow | review terms, then activate |
| apple_newsroom | 0.9 | ok, 20 | allow / allow | review terms, then activate |
| microsoft_news | 0.9 | ok, 10 | allow / allow | review terms, then activate |
| nvidia_news | 0.9 | ok, 20 | allow / allow | review terms, then activate |
| meta_news | 0.9 | ok, 10 | allow / allow | review terms, then activate |
| treasury_press | 1.0 | **no feed** | — | keep disabled (GovDelivery email only) |
| noaa_active_alerts | 1.0 | — | **disallow** | keep disabled (robots.txt `Disallow: /`) |
| Reuters, AP, Bloomberg, FT, CNBC | 0.5–0.7 | none | — | keep disabled until licensed |

## Changes made during validation

- **SEC split into 8-K and 6-K.** The all-forms current feed is dominated by insider
  Form 4s, which are noise for tiering. 8-K carries US issuers' current events.
  6-K is a new source for foreign private issuers such as TSMC and ASML, which file
  6-K rather than 8-K. Both feed the §8.1 fast path.
- **CFTC** moved its feed to `/RSS/RSSGP/rssgp.xml`; the old URL returns 404.
- **Treasury** retired `press-releases.xml` (404). The site-wide `rss.xml` carries
  FAQs and careers pages, not releases, so it was rejected; `feed_url` is null.
- **EIA** returned 406 because the poller's `Accept` header omitted `text/xml`.
  Fixed in the poller.
- **ECB** failed TLS verification against the macOS Python trust store although the
  chain is valid. Feeds and bodies now verify with Mozilla's CA bundle (`certifi`,
  now a declared dependency), so laptops and production behave the same. Its "market
  information" feed carried machine operational messages (TIPS/T2 status), not news,
  so the source is now `ecb_press` (`/rss/press.html`): press releases, speeches and
  policy decisions.

## Body access found in the real-data run

- **Federal Register** blocks automated HTML requests and serves a "request access"
  page. Bodies are read from the published full-text path
  (`/documents/full_text/text/<date>/<number>.txt`).
- **SEC** feed links are filing index pages. Bodies are the primary form document
  plus EX-99 press exhibits (6-K news lives there).
- **USGS** and **DOJ** article pages are script-rendered or bot-gated (under 300
  characters of text). They are refused as "no usable text": metadata only, with a
  title+snippet embedding when tier 1.

## Rights notes (§7.4: we store bodies privately, show title, our summary and a link)

- US federal agency works are generally not subject to copyright (17 U.S.C. §105).
  SEC's fair-access policy requires a declared User-Agent and at most 10 requests per
  second; the poller declares one and polls every 5 minutes.
- ECB, Bank of England and the company newsrooms publish these feeds for
  redistribution, but each has its own terms of use. **A founder should read them
  before activation**; this is a judgment call, not something validation can settle.

## How to activate (after approval)

Set `active: true` on the approved sources, set `TEVARTA_HTTP_CONTACT`, and run
`scripts/validate_sources.py` once more. Then watch `scripts/ingest_report.py` for the
first days: hop-3 promotion (T23) makes countries live, so tier-1 volume can be high.
