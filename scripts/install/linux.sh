#!/usr/bin/env bash
# Install notioncode_mcp as two always-on systemd services.
#
# Idempotent: safe to re-run after a git pull, after adding a Notion session or
# after updating the Codex extension.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME_DIR="${ROOT}/.runtime"
ENV_DIR="${RUNTIME_DIR}/env"
BRIDGE_ENV="${ENV_DIR}/bridge.env"
MCP_ENV="${ENV_DIR}/mcp-runtime.env"
LEGACY_RUNTIME_ENV="${ROOT}/services/mcp-runtime/.env"
LEGACY_UNITS=(notion-code-mcp.service notion-fable-proxy.service)
UNITS=(notioncode-runtime.service notioncode-bridge.service)

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root (sudo -H scripts/install/linux.sh)." >&2
  exit 1
fi

for command_name in python3 node npm openssl getent runuser systemctl curl; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Required command is missing: ${command_name}" >&2
    exit 1
  fi
done
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Python 3.10 or newer is required." >&2; exit 1; }
node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 18 ? 0 : 1)' \
  || { echo "Node.js 18 or newer is required." >&2; exit 1; }

SERVICE_USER="${NOTIONCODE_USER:-${SUDO_USER:-root}}"
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  echo "Linux user does not exist: ${SERVICE_USER}" >&2
  exit 1
fi
SERVICE_GROUP="$(id -gn "${SERVICE_USER}")"
USER_HOME="$(getent passwd "${SERVICE_USER}" | cut -d: -f6)"
if [[ -z "${USER_HOME}" || ! -d "${USER_HOME}" ]]; then
  echo "Could not resolve a home directory for ${SERVICE_USER}." >&2
  exit 1
fi
CODE_ROOT_EXPLICIT="${CODE_ROOT:-}"
CODE_ROOT="${CODE_ROOT:-${USER_HOME}}"
ACCOUNT_HOME="${USER_HOME}/.notionagents"
CODEX_HOME="${USER_HOME}/.codex"
USER_SHARE="${USER_HOME}/.local/share"
OPENCODE_HOME="${RUNTIME_DIR}/opencode"
VENV="${RUNTIME_DIR}/notion-agent-cli-venv"
BRIDGE_PORT="${NOTION_BRIDGE_PORT:-8765}"
RUNTIME_PORT="${NOTION_MCP_RUNTIME_PORT:-8787}"

run_as_service_user() {
  if [[ "${SERVICE_USER}" == "root" ]]; then
    HOME="${USER_HOME}" "$@"
  else
    runuser -u "${SERVICE_USER}" -- env HOME="${USER_HOME}" "$@"
  fi
}

echo "==> Preparing directories"
mkdir -p "${RUNTIME_DIR}" "${ENV_DIR}" "${RUNTIME_DIR}/logs" "${OPENCODE_HOME}" \
  "${ACCOUNT_HOME}/accounts" "${CODEX_HOME}" "${USER_SHARE}"
chown "${SERVICE_USER}:${SERVICE_GROUP}" \
  "${ACCOUNT_HOME}" "${CODEX_HOME}" "${USER_SHARE}" "${OPENCODE_HOME}" \
  "${RUNTIME_DIR}" "${ENV_DIR}" "${RUNTIME_DIR}/logs"
chmod 700 "${ACCOUNT_HOME}" "${ACCOUNT_HOME}/accounts" "${ENV_DIR}"

echo "==> Installing Python dependencies"
if [[ ! -x "${VENV}/bin/python" ]]; then
  python3 -m venv "${VENV}"
fi
"${VENV}/bin/pip" install --disable-pip-version-check --quiet -r "${ROOT}/requirements.txt"
"${VENV}/bin/pip" install --disable-pip-version-check --quiet --no-deps -e "${ROOT}"

echo "==> Installing Node dependencies"
npm --prefix "${ROOT}/services/mcp-runtime" ci --omit=dev --no-audit --no-fund
npm --prefix "${ROOT}/services/notion-private-mcp" ci --omit=dev --no-audit --no-fund
npm --prefix "${OPENCODE_HOME}" install --no-audit --no-fund \
  @ai-sdk/openai-compatible @opencode-ai/plugin

echo "==> Writing service environment"
write_env() {
  local path="$1"
  shift
  printf '%s\n' "$@" > "${path}"
  chmod 600 "${path}"
  chown "${SERVICE_USER}:${SERVICE_GROUP}" "${path}"
}

MCP_SECRET=""
if [[ -f "${MCP_ENV}" ]]; then
  MCP_SECRET="$(sed -n 's/^MCP_PATH_SECRET=//p' "${MCP_ENV}" | head -n1)"
elif [[ -f "${LEGACY_RUNTIME_ENV}" ]]; then
  # Carry the secret and CODE_ROOT over from a pre-2.0 install so an already
  # working Codex/OpenCode configuration keeps pointing at the same endpoint.
  MCP_SECRET="$(sed -n 's/^MCP_PATH_SECRET=//p' "${LEGACY_RUNTIME_ENV}" | head -n1)"
  legacy_code_root="$(sed -n 's/^CODE_ROOT=//p' "${LEGACY_RUNTIME_ENV}" | head -n1)"
  if [[ -n "${legacy_code_root}" && -z "${CODE_ROOT_EXPLICIT}" ]]; then
    CODE_ROOT="${legacy_code_root}"
  fi
  echo "    migrated the MCP secret from services/mcp-runtime/.env"
fi
if [[ -z "${MCP_SECRET}" ]]; then
  MCP_SECRET="$(openssl rand -hex 32)"
fi

write_env "${MCP_ENV}" \
  "MCP_PATH_SECRET=${MCP_SECRET}" \
  "CODE_ROOT=${CODE_ROOT}" \
  "HOST=127.0.0.1" \
  "PORT=${RUNTIME_PORT}"

write_env "${BRIDGE_ENV}" \
  "NOTION_AGENT_HOME=${ACCOUNT_HOME}" \
  "NOTION_BRIDGE_HOST=127.0.0.1" \
  "NOTION_BRIDGE_PORT=${BRIDGE_PORT}" \
  "NOTION_MCP_RUNTIME_URL=http://127.0.0.1:${RUNTIME_PORT}/mcp/${MCP_SECRET}" \
  "CODE_ROOT=${CODE_ROOT}" \
  "NOTION_FORCE_MODEL=${NOTION_FORCE_MODEL:-opus-5}" \
  "NOTION_LOG_LEVEL=${NOTION_LOG_LEVEL:-INFO}" \
  "NOTION_INFERENCE_TIMEOUT_SECONDS=${NOTION_INFERENCE_TIMEOUT_SECONDS:-180}"

echo "==> Installing model aliases and migrating accounts"
run_as_service_user node "${ROOT}/scripts/codex/install-model-aliases.mjs" \
  "${ROOT}/state-template/.notionagents/models.json" "${ACCOUNT_HOME}/models.json"
chmod 600 "${ACCOUNT_HOME}/models.json"
run_as_service_user "${VENV}/bin/python" -m notion_bridge.accounts.migrate "${ACCOUNT_HOME}"

NOTION_MCP_ENABLED=false
if [[ -f "${ACCOUNT_HOME}/notion_account.json" ]] \
  || [[ -n "$(find "${ACCOUNT_HOME}/accounts" -maxdepth 1 -type f -name '*.json' -print -quit)" ]]; then
  NOTION_MCP_ENABLED=true
fi

echo "==> Rendering client configuration"
run_as_service_user node "${ROOT}/scripts/render-config.mjs" \
  "${ROOT}/config/opencode.jsonc" "${OPENCODE_HOME}/opencode.jsonc" "${ROOT}" "${USER_HOME}"
run_as_service_user node "${ROOT}/scripts/codex/install-config.mjs" \
  "${ROOT}/config/codex-cli-config.toml" "${CODEX_HOME}/config.toml" "${ROOT}" "${USER_HOME}" \
  "${NOTION_MCP_ENABLED}"
run_as_service_user node "${ROOT}/scripts/codex/patch-webview.mjs" "${USER_HOME}"

ln -sfn "${VENV}" "${USER_SHARE}/notion-agent-cli-venv"
chown -h "${SERVICE_USER}:${SERVICE_GROUP}" "${USER_SHARE}/notion-agent-cli-venv"

echo "==> Installing systemd units"
for legacy in "${LEGACY_UNITS[@]}"; do
  if [[ -f "/etc/systemd/system/${legacy}" ]]; then
    systemctl disable --now "${legacy}" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/${legacy}"
    echo "    removed superseded unit ${legacy}"
  fi
done
for unit in "${UNITS[@]}" notioncode.target; do
  node "${ROOT}/scripts/render-config.mjs" \
    "${ROOT}/deploy/systemd/${unit}" "/etc/systemd/system/${unit}" \
    "${ROOT}" "${USER_HOME}" "${SERVICE_USER}"
done
systemctl daemon-reload
systemctl enable "${UNITS[@]}" notioncode.target >/dev/null
systemctl restart "${UNITS[@]}"

echo "==> Waiting for the bridge to answer"
health=""
for _ in $(seq 1 30); do
  if health="$(curl -fsS "http://127.0.0.1:${BRIDGE_PORT}/healthz" 2>/dev/null)"; then
    break
  fi
  sleep 1
done
if [[ -z "${health}" ]]; then
  echo "The bridge did not become healthy. Inspect:" >&2
  echo "  journalctl -u notioncode-bridge.service -n 50 --no-pager" >&2
  exit 1
fi

echo
echo "Installation complete."
echo "  project root      ${ROOT}"
echo "  service user      ${SERVICE_USER}"
echo "  code root         ${CODE_ROOT}"
echo "  accounts          ${ACCOUNT_HOME}"
echo "  bridge            http://127.0.0.1:${BRIDGE_PORT}  (/healthz /readyz /metrics)"
echo "  coding runtime    http://127.0.0.1:${RUNTIME_PORT} (/healthz)"
echo "  codex config      ${CODEX_HOME}/config.toml"
echo "  opencode profile  ${OPENCODE_HOME}"
echo "  services          systemctl status notioncode.target"

if [[ "${NOTION_MCP_ENABLED}" == "false" ]]; then
  cat <<EOF

No Notion session is configured yet, so the notion-private MCP server stays
disabled and the bridge answers 503. Add a session, then re-run this installer:

  sudo -u ${SERVICE_USER} -H ${VENV}/bin/notion-agent init --token-v2 - \\
    --account ${ACCOUNT_HOME}/notion_account.json
  sudo -u ${SERVICE_USER} -H ${VENV}/bin/notion-agent doctor \\
    --account ${ACCOUNT_HOME}/notion_account.json --json
  sudo -H ${ROOT}/scripts/install/linux.sh

Paste only the token_v2 value on stdin, press Enter, then Ctrl-D.
EOF
fi
