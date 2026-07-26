#!/usr/bin/env bash
# Run the bridge in the foreground with the installed service environment.
# Useful for debugging; systemd owns the long-running deployment.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ROOT}/.runtime/env/bridge.env"
PYTHON="${ROOT}/.runtime/notion-agent-cli-venv/bin/python"

if [[ ! -x "${PYTHON}" ]]; then
  echo "Virtual environment is missing. Run: sudo -H ./scripts/install/linux.sh" >&2
  exit 1
fi
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
else
  echo "warning: ${ENV_FILE} not found; using the current environment" >&2
fi

export NOTIONCODE_ROOT="${ROOT}"
export PYTHONUNBUFFERED=1
cd "${ROOT}"
exec "${PYTHON}" -m notion_bridge "$@"
