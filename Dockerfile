# One image for the API, Celery worker, Celery beat, and one-off scripts (multi-arch: amd64, arm64).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Editable install keeps the code under /app/backend, so the repo-relative paths the
# application resolves (config/, graph/, benchmarks/) point at /app.
COPY pyproject.toml ./
COPY backend ./backend
RUN pip install -e .

COPY alembic.ini ./
COPY migrations ./migrations
COPY config ./config
COPY graph ./graph
# The API's error envelopes are read from the frozen v1 fixtures at runtime.
COPY fixtures/v1 ./fixtures/v1
COPY scripts ./scripts

# Non-root. Bodies are private processing output (§7.4); mount a volume at this path.
RUN useradd --uid 10001 --create-home tevarta \
    && mkdir -p /var/lib/tevarta/bodies \
    && chown -R tevarta /var/lib/tevarta
USER 10001

ENV PYTHONPATH=/app/backend \
    TEVARTA_BODY_STORE_DIR=/var/lib/tevarta/bodies

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
