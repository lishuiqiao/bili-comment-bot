# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.11.2 AS uv
FROM python:3.11.14-slim-bookworm AS build
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_NOTICES.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.11.14-slim-bookworm AS runtime
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BILI_BOT_DATA_DIR=/data BILI_BOT_WEB_CONFIG=/data/settings.web.json
RUN groupadd --gid 10001 bot && useradd --uid 10001 --gid 10001 --no-create-home bot \
    && mkdir /data && chown bot:bot /data && chmod 700 /data
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY LICENSE THIRD_PARTY_NOTICES.md /app/
COPY config.example.toml /app/config.toml
USER 10001:10001
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "from bili_comment_bot.healthcheck import main; main()", "/app/config.toml"]
ENTRYPOINT ["bili-comment-bot", "--config", "/app/config.toml"]
CMD ["run"]
