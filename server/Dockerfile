# Stage 1: fetch the Claude Code CLI. npm installs a self-contained native binary, so Node is not needed at runtime.
FROM node:22-slim AS claude
ARG CLAUDE_CODE_VERSION=2.1.283
RUN npm install -g --omit=dev "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" && npm cache clean --force

FROM python:3.13-slim AS test
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY tests ./tests
RUN pip install --no-cache-dir '.[dev]'
RUN ruff check . && ruff format --check . && pytest -q

FROM python:3.13-slim AS runtime

# The classifier shells out to `claude -p` (see docs/server.md). Copied from the stage above, then linked onto PATH.
COPY --from=claude /usr/local/lib/node_modules/@anthropic-ai /usr/local/lib/node_modules/@anthropic-ai
RUN ln -s /usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe /usr/local/bin/claude

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir '.[otel]'

# config.env is the config contract (defaults/required, see the file itself), including the
# classifier staying off by default: set SHOPPING_LIST_CLASSIFIER=claude and give it
# ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN to enable it. The CLI keeps its state under $HOME (/data).
# docker-entrypoint.sh resolves config.env against the real environment before the app starts.
COPY config.env docker-entrypoint.sh ./
RUN chmod 755 docker-entrypoint.sh
VOLUME /data
EXPOSE 8000

RUN useradd --system --home /data shopping && mkdir -p /data && chown shopping /data
USER shopping
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "shopping_list.main:app", "--host", "0.0.0.0", "--port", "8000"]
