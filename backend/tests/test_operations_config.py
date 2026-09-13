import json
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_usage_policy_has_hard_search_cap_and_reported_components() -> None:
    policy = json.loads((REPOSITORY_ROOT / "config/usage_limits.json").read_text())

    assert policy["limits"]["search"]["monthly_query_cap"] > 0
    assert policy["limits"]["search"]["enforcement"] == "hard_cap_before_dispatch"
    assert policy["alerts"]["search_usage_ratio"] == 0.8
    assert {"search", "embeddings", "postgres", "neo4j", "redis", "r2"} <= set(
        policy["reporting_components"]
    )


def test_backup_policy_is_daily_and_requires_production_restore_proof() -> None:
    policy = json.loads((REPOSITORY_ROOT / "infra/backup-schedules.json").read_text())

    for service in ("postgres", "neo4j"):
        production = policy["environments"]["production"][service]
        assert production["frequency"] == "daily"
        assert production["restore_test_required_before_beta"] is True
