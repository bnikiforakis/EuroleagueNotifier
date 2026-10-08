# syntax=docker/dockerfile:1
FROM python:3.13-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim-trixie
RUN useradd --system --uid 10001 app && mkdir /data && chown app /data
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 DB_PATH=/data/euroleague_notifier.db HEARTBEAT_FILE=/tmp/heartbeat
USER app
VOLUME /data
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import os, sys, time; sys.exit(time.time() - os.path.getmtime('/tmp/heartbeat') > 1800)"
CMD ["euroleague-notifier"]
