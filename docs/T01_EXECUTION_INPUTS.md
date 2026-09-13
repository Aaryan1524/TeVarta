# T01 — execution inputs

**Owner:** Aaryan prepares; Aaditya confirms shared-contract consequences by 2026-09-16.

This records the Day 1 inputs needed to build Phase 1. It does not revise the frozen MVP. `config/sources.yaml` is the selected source registry; T19 must prove each adapter and rights condition before production polling.

## Source policy

- Fifteen primary/company sources form the proposed beta backbone. They all use `tier_value` 1.0 or 0.9, so a validated article would receive the documented high-tier fetch override. T01 deliberately leaves every source disabled: T19 must validate its endpoint, robots/feed terms, and body-processing/storage permission before setting `active: true`.
- Reuters, AP, Bloomberg, Financial Times, and CNBC are selected discovery candidates but remain disabled until their delivery terms and content rights are approved. This leaves independent wire/secondary coverage as an explicit launch gap. The app will continue to show only title, Te Varta's summary, and a link out.
- `reliability` is an operational triage signal only in beta; confidence and importance use the frozen `tier_value` and publisher-group formulas.
- A source's `publisher_group` denotes the actual publisher, not its domain. For Federal Register, syndicated articles, and republished company releases, T19 must retain the originating publisher/agency and deduplicate equivalent evidence. A distributor feed and an originating-agency feed cannot count as independent corroboration.

## Initial curation targets

This is a launch proxy for the waitlist. Replace only the ordering—not the evidence rules—when either 10 waitlist survey responses or the first 25 user seeds are available.

| Priority | Entity | Ticker | Why it starts in the first 30 |
|---:|---|---|---|
| 1 | Apple | AAPL | Consumer hardware and semiconductor dependency spine |
| 2 | NVIDIA | NVDA | AI compute, Taiwan, and memory exposure |
| 3 | Microsoft | MSFT | Cloud, enterprise software, and AI infrastructure |
| 4 | Amazon | AMZN | AWS, logistics, and retail dependencies |
| 5 | Alphabet | GOOGL | Cloud, advertising, and AI infrastructure |
| 6 | Meta Platforms | META | AI compute and advertising exposure |
| 7 | Tesla | TSLA | Batteries, China, manufacturing, and logistics |
| 8 | Broadcom | AVGO | Networking and semiconductor concentration |
| 9 | Advanced Micro Devices | AMD | AI compute and TSMC dependency |
| 10 | Qualcomm | QCOM | Mobile semiconductor supply chain |
| 11 | Intel | INTC | Domestic semiconductor manufacturing |
| 12 | Micron Technology | MU | Memory supply chain |
| 13 | Taiwan Semiconductor Manufacturing | TSM | Foundry dependency hub |
| 14 | ASML | ASML | EUV single-point-of-failure hub |
| 15 | Applied Materials | AMAT | Semiconductor equipment dependency |
| 16 | Lam Research | LRCX | Semiconductor equipment dependency |
| 17 | KLA | KLAC | Semiconductor process-control dependency |
| 18 | Synopsys | SNPS | Chip-design tooling dependency |
| 19 | Cadence Design Systems | CDNS | Chip-design tooling dependency |
| 20 | Marvell Technology | MRVL | Networking and data-center silicon |
| 21 | Cisco | CSCO | Enterprise networking supply chain |
| 22 | Oracle | ORCL | Enterprise cloud and data infrastructure |
| 23 | Salesforce | CRM | Enterprise software dependency |
| 24 | SanDisk | SNDK | Flash-memory dependency and required alias-resolution fixture |
| 25 | JPMorgan Chase | JPM | Financial-system and regulatory exposure |
| 26 | Visa | V | Payments-network exposure |
| 27 | Mastercard | MA | Payments-network exposure |
| 28 | Exxon Mobil | XOM | Energy and commodity exposure |
| 29 | Boeing | BA | Aerospace manufacturing supply chain |
| 30 | UnitedHealth Group | UNH | Healthcare regulation and provider exposure |

## Embeddings

Use `text-embedding-3-small` with `dimensions: 1024` for both article and event embeddings. This preserves the frozen `VECTOR(1024)` schema, supports the metadata/body policy, and fits the beta cost constraint. The model's output dimensions are configurable in the embeddings API; record token usage per call and reevaluate only after the labelled clustering benchmark exists.

## Proposed initial relation weights

These are Aaryan's proposed relation defaults pending founder confirmation by 2026-09-16. Evidence-specific curated weights still override them, and the Week 8 fit may revise them using observed feedback. They are separate from the frozen relevance calculation: `PathScore`, importance, and freshness carry weight, while portfolio exposure, source reliability, novelty, and explicit interest are stored at weight zero.

| Relation | Default | Rationale |
|---|---:|---|
| `DEPENDS_ON` | 0.90 | Direct operational dependency |
| `SUPPLIES` | 0.85 | Direct supplier relationship |
| `SOLE_SOURCE_FOR` | 0.95 | Concentrated, high-signal dependency |
| `MANUFACTURES_AT` | 0.85 | Facility disruption exposure |
| `EXPOSED_TO` | 0.80 | Geographic, commodity, or regulatory exposure |
| `CUSTOMER_OF` | 0.65 | Commercial demand linkage |
| `COMPETES_WITH` | 0.25 | Indirect relevance unless evidence strengthens it |
| `PRODUCES` | 0.75 | Product/input linkage |
| `SUBSIDIARY_OF` | 0.95 | Corporate control relationship |
| `OPERATED_BY` | 0.90 | Direct operational responsibility |
| `OPERATES_IN` | 0.75 | Location exposure |
| `REGULATED_BY` | 0.65 | Regulatory exposure |
| `LISTED_AS` | 0.99 | Security-to-issuer identity bridge |
| `IN_INDUSTRY` | 0.20 | Taxonomy only; not a strong impact path |

## Execution conflicts — proposed or escalated

| ID | Disposition | Deadline / owner |
|---|---|---|
| SC1 — probe rate | **Proposal:** hold 1-in-6 through Weeks 7–8; a 1-in-10 change needs a measured sample-size review, not a calendar-only change. | Both founders confirm by 2026-09-16; Aaryan implements in T40 |
| SC2 — Week 2 demo path | **Evidence constraint:** demonstrate `Apple → TSMC → ASML`, `TSMC → Taiwan`, and `ASML → Netherlands` only when backed by evidence. Do not fabricate `ASML → Taiwan`. | Both founders confirm by 2026-09-16; Aaryan enforces in T15 |
| SC3 — security routes | **Proposal:** add `POST /v1/auth/refresh`, `POST /v1/auth/sign-out`, and `DELETE /v1/account` to the T03 contract. | Aaditya drafts; both founders approve by 2026-09-16 |
| SC4 — portfolio schema | **Proposal:** treat `user_seeds(user_id, entity_id, weight, source)` as the normalized beta holding layer. Retain `source='broker'`; add account/holding tables only with broker integration after beta. | Both founders confirm by 2026-09-16; enforce in T05 after approval |

## T01 verification

- `config/sources.yaml` has 20 selected sources, each with a publisher group, tier value, reliability, cadence, and activation state.
- The 15-source primary/company backbone and five secondary candidates remain disabled until T19 validates endpoint and rights conditions; publisher-origin attribution prevents distributor copies inflating corroboration.
- The target list contains 30 initial entities and has an explicit seed-data replacement trigger.
- The embedding dimension matches the frozen Postgres schema, every graph relation has a proposed initial default, and all four execution conflicts have a recorded proposal or evidence constraint with founder-confirmation deadlines.
