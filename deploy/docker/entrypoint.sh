#!/usr/bin/env bash
# Start both services and make the container exit if either one dies, so the
# container runtime's restart policy owns recovery.
set -euo pipefail

: "${NOTIONCODE_ROOT:=/app}"
: "${NOTION_AGENT_HOME:=/state/.notionagents}"
: "${CODE_ROOT:=/workspace}"
: "${NOTION_BRIDGE_HOST:=0.0.0.0}"
: "${NOTION_BRIDGE_PORT:=8765}"
: "${PORT:=8787}"
export NOTIONCODE_ROOT NOTION_AGENT_HOME CODE_ROOT NOTION_BRIDGE_HOST NOTION_BRIDGE_PORT PORT

if [[ -z "${MCP_PATH_SECRET:-}" ]]; then
  secret_file="${NOTION_AGENT_HOME}/mcp-path-secret"
  if [[ ! -s "${secret_file}" ]]; then
    mkdir -p "${NOTION_AGENT_HOME}"
    python -c 'import secrets; print(secrets.token_hex(32))' > "${secret_file}"
    chmod 600 "${secret_file}"
  fi
  MCP_PATH_SECRET="$(cat "${secret_file}")"
  export MCP_PATH_SECRET
fi
export NOTION_MCP_RUNTIME_URL="http://127.0.0.1:${PORT}/mcp/${MCP_PATH_SECRET}"

if [[ ! -f "${NOTION_AGENT_HOME}/models.json" ]]; then
  node "${NOTIONCODE_ROOT}/scripts/codex/install-model-aliases.mjs" \
    "${NOTIONCODE_ROOT}/state-template/.notionagents/models.json" \
    "${NOTION_AGENT_HOME}/models.json" 2>/dev/null || true
fi

node "${NOTIONCODE_ROOT}/services/mcp-runtime/src/server.js" &
runtime_pid=$!
python -m notion_bridge &
bridge_pid=$!

terminate() {
  kill -TERM "${runtime_pid}" "${bridge_pid}" 2>/dev/null || true
  wait "${runtime_pid}" "${bridge_pid}" 2>/dev/null || true
  exit 0
}
trap terminate SIGTERM SIGINT

# `wait -n` returns as soon as either service exits; the container then stops
# and is restarted as a whole, which keeps the two halves in sync.
wait -n "${runtime_pid}" "${bridge_pid}"
status=$?
echo "notioncode_mcp: a service exited with status ${status}; stopping the container" >&2
terminate
exit "${status}"
