"""Celery application boundary; task implementations land with their owners."""

from celery import Celery

celery_app = Celery("tevarta")
celery_app.config_from_object("app.worker_settings")
