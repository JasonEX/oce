FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Install dependencies first so source changes do not invalidate the dependency layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY alembic.ini ./
# The migration scripts live under src/oce/alembic and ship with src
COPY src ./src

RUN uv sync --locked --no-dev

EXPOSE 8986

CMD ["uv", "run", "uvicorn", "oce.main:app", "--host", "0.0.0.0", "--port", "8986"]
