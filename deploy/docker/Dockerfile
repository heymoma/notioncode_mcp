# Headless deployment of both services in one image.
#
# This image serves API clients (Claude Code, OpenCode, scripts). It cannot
# patch a local VS Code extension or write ~/.codex, so Codex-in-VS-Code
# deployments should use the native installer instead.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NODE_ENV=production \
    NOTIONCODE_ROOT=/app \
    NOTION_AGENT_HOME=/state/.notionagents \
    CODE_ROOT=/workspace

RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs npm curl ca-certificates git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python bridge dependencies first, so source edits do not invalidate them.
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

COPY services/mcp-runtime/package.json services/mcp-runtime/package-lock.json \
     ./services/mcp-runtime/
RUN npm --prefix services/mcp-runtime ci --omit=dev

COPY services/notion-private-mcp/package.json \
     services/notion-private-mcp/package-lock.json \
     ./services/notion-private-mcp/
RUN npm --prefix services/notion-private-mcp ci --omit=dev

COPY src ./src
COPY services ./services
COPY config ./config
COPY scripts ./scripts
COPY state-template ./state-template
COPY deploy/docker/entrypoint.sh /usr/local/bin/notioncode-entrypoint
RUN pip install --no-cache-dir --no-deps -e . \
 && chmod +x /usr/local/bin/notioncode-entrypoint \
 && mkdir -p /state/.notionagents /workspace

VOLUME ["/state", "/workspace"]
EXPOSE 8765 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8765/livez || exit 1

ENTRYPOINT ["/usr/local/bin/notioncode-entrypoint"]
