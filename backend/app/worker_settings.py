import os

broker_url = os.getenv("TEVARTA_REDIS_URL", "redis://localhost:6379/0")
result_backend = broker_url
task_always_eager = os.getenv("TEVARTA_CELERY_EAGER", "false").lower() == "true"
task_serializer = "json"
result_serializer = "json"
accept_content = ["json"]
imports = ("app.discovery", "app.expansion", "app.bodies", "app.quarantine")
beat_schedule = {
    "poll-sources-every-five-minutes": {
        "task": "app.discovery.poll_sources",
        "schedule": 300.0,
    },
    "dispatch-entity-expansions": {
        "task": "app.expansion.dispatch_expansions",
        "schedule": 15.0,
    },
    "purge-expired-articles-hourly": {
        "task": "app.quarantine.purge_expired_articles",
        "schedule": 3600.0,
    },
    "entity-sweep-daily": {
        "task": "app.discovery.entity_sweep",
        "schedule": 86400.0,
    },
}
