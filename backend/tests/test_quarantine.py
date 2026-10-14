"""T22 routing and sampling rules without a database."""

import hashlib
from datetime import UTC, datetime

import pytest
from app.quarantine import AUDIT_RATE_PERCENT, audit_sampled, body_mode, week_start


def test_audit_sample_is_reproducible_and_about_five_percent():
    hashes = [hashlib.sha256(f"https://example.org/{i}".encode()).hexdigest()
              for i in range(20000)]
    sampled = [audit_sampled(value) for value in hashes]
    assert sampled == [audit_sampled(value) for value in hashes]
    assert 0.045 <= sum(sampled) / len(sampled) <= 0.055
    assert AUDIT_RATE_PERCENT == 5


@pytest.mark.parametrize(("tier", "tier_value", "sampled", "mode"), [
    (1, 0.5, False, "default"),      # tier 1: body fetched
    (1, 1.0, True, "default"),
    (2, 0.5, False, "default"),      # tier 2: title+snippet embedding only
    (2, 0.9, False, "override"),     # company IR: always fetched
    (3, 1.0, False, "override"),     # SEC/regulator: always fetched
    (3, 0.7, True, "audit"),         # 5% of tier 3
    (3, 0.7, False, None),           # metadata only
])
def test_each_article_gets_exactly_its_tier_treatment(tier, tier_value, sampled, mode):
    assert body_mode(tier, tier_value, sampled) == mode


def test_demand_weeks_start_on_monday_in_utc():
    assert week_start(datetime(2026, 10, 15, 23, tzinfo=UTC)).date().isoformat() == "2026-10-12"
    assert week_start(datetime(2026, 10, 12, 0, tzinfo=UTC)).date().isoformat() == "2026-10-12"
