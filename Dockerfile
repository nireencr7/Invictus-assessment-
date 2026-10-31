# One image, three entrypoints: api (default), worker, migrate.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
COPY data ./data
RUN uv sync --frozen --no-dev && useradd --system --uid 10001 app
USER app
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8080
# Cloud Run injects PORT.
CMD ["sh", "-c", "exec python -m riverbend api --port ${PORT:-8080}"]
