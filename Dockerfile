FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    STATE_FILE=/data/state.json

WORKDIR /app

# Install dependencies first (cached layer)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

# Then the project itself
COPY src ./src
RUN uv sync --locked --no-dev

RUN useradd --create-home --uid 1000 app && mkdir /data && chown app /data
USER app
VOLUME /data

CMD ["/app/.venv/bin/iust-mail"]
