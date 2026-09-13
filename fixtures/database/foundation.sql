-- Synthetic rows for migration integrity tests. No real identities or content.
INSERT INTO users (id, public_id, apple_sub) VALUES
 ('00000000-0000-0000-0000-000000000001', 'usr_test_a', 'synthetic-sub-a'),
 ('00000000-0000-0000-0000-000000000002', 'usr_test_b', 'synthetic-sub-b');
INSERT INTO entities (entity_id, canonical_name, type) VALUES
 ('ent_apple', 'Apple Inc.', 'Company'), ('ent_fruit', 'Apple fruit', 'Topic');
INSERT INTO entity_aliases (alias_norm, entity_id, source, confidence) VALUES
 ('apple', 'ent_apple', 'fixture', 0.7), ('apple', 'ent_fruit', 'fixture', 0.7);
INSERT INTO sources (id, domain, publisher_group, tier_value, reliability, poll_interval_s)
 VALUES ('fixture_sec', 'example.invalid', 'fixture_sec', 1.0, 0.99, 300);
INSERT INTO articles (id, public_id, url_hash, url, source_id, title, published_at)
 VALUES ('00000000-0000-0000-0000-000000000003', 'art_test', 'fixture-url-hash',
 'https://example.invalid/filing', 'fixture_sec', 'Synthetic filing', now());
INSERT INTO events (id, public_id, canonical_title) VALUES
 ('00000000-0000-0000-0000-000000000004', 'evt_test', 'Synthetic event'),
 ('00000000-0000-0000-0000-000000000005', 'evt_other', 'Synthetic second event');
INSERT INTO event_articles VALUES
 ('00000000-0000-0000-0000-000000000004', '00000000-0000-0000-0000-000000000003');
INSERT INTO event_entities VALUES
 ('00000000-0000-0000-0000-000000000004', 'ent_apple', 'subject');
INSERT INTO user_seeds (user_id, entity_id, source)
 SELECT id, 'ent_apple', 'manual' FROM users;
INSERT INTO devices (public_id, user_id, apns_token_enc, platform)
 SELECT 'dev_' || public_id, id, decode('deadbeef', 'hex'), 'ios' FROM users;
INSERT INTO why_cache (event_id, path_signature, text, method) VALUES
 ('00000000-0000-0000-0000-000000000004', 'fixture-path', 'Synthetic explanation', 'template');
INSERT INTO user_feed (user_id, event_id, score, components, path_json,
                      slot_type, slot_position, channel, shadow_score, shadow_rank)
 SELECT id, '00000000-0000-0000-0000-000000000004', 74.375,
 '{"path_score":0.6075,"importance":0.8,"freshness":1,"portfolio_exposure":0.3,
   "source_reliability":0.99,"novelty":0.2,"explicit_interest":1}',
 '{"entity_ids":["ent_apple"]}', 'graph', 1, 'feed', 40, 2 FROM users;
INSERT INTO feedback (user_id, event_id, action, slot_type, slot_position, idempotency_key, ts)
 SELECT id, '00000000-0000-0000-0000-000000000004', 'card_impression', 'graph', 1,
 'fixture-impression', now() FROM users;
INSERT INTO notification_log (user_id, event_id)
 SELECT id, '00000000-0000-0000-0000-000000000004' FROM users;
INSERT INTO event_claims (event_id, event_revision, article_id, text, directness) VALUES
 ('00000000-0000-0000-0000-000000000004', 1, '00000000-0000-0000-0000-000000000003',
 'Synthetic claim.', 'stated');
INSERT INTO usage_records (request_id, component, outcome, quantity, unit, estimated_cost_usd)
 VALUES ('00000000-0000-0000-0000-000000000006', 'search', 'success', 1, 'query', 0.001);
