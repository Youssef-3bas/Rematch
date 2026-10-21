FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DATABASE_URL=sqlite:////tmp/rematch/rematch.db

WORKDIR /app

RUN groupadd --system --gid 10001 rematch \
    && useradd --system --uid 10001 --gid rematch --home-dir /app rematch \
    && mkdir -p /tmp/rematch \
    && chown rematch:rematch /tmp/rematch

COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

RUN pip install --no-cache-dir .

USER rematch
EXPOSE 8000

CMD ["sh", "-c", "set -eu; echo '[startup] validating required configuration'; python -c 'from app.core.config import get_settings; get_settings()' || { echo '[startup] configuration validation failed'; exit 1; }; echo '[startup] applying database migrations'; python -m alembic upgrade head || { echo '[startup] database migrations failed'; exit 1; }; echo '[startup] seeding synthetic match data'; rematch-seed || { echo '[startup] synthetic data seeding failed'; exit 1; }; echo '[startup] starting Uvicorn on 0.0.0.0:${PORT:-8000}'; exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
