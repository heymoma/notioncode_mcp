#!/usr/bin/env bash
# Run the coding-tools MCP runtime in the foreground.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ROOT}/.runtime/env/mcp-runtime.env"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
else
  echo "warning: ${ENV_FILE} not found; using the current environment" >&2
fi

cd "${ROOT}/services/mcp-runtime"
exec node src/server.js "$@"
