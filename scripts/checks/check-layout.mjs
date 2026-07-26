#!/usr/bin/env node
/**
 * Guard the repository layout that the installers, units and docs assume.
 *
 * The invariant this protects is a single shared implementation: only process
 * launch may differ between Linux and Windows.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const legacyProjectPath = ["", "root", "notioncode_mcp"].join("/");

const requiredShared = [
  // Python bridge package.
  "src/notion_bridge/__init__.py",
  "src/notion_bridge/__main__.py",
  "src/notion_bridge/app.py",
  "src/notion_bridge/settings.py",
  "src/notion_bridge/service.py",
  "src/notion_bridge/metrics.py",
  "src/notion_bridge/sd_notify.py",
  "src/notion_bridge/accounts/pool.py",
  "src/notion_bridge/accounts/migrate.py",
  "src/notion_bridge/state/turn_affinity.py",
  "src/notion_bridge/state/conversation_segments.py",
  "src/notion_bridge/notion/images.py",
  "src/notion_bridge/notion/models.py",
  "src/notion_bridge/planner/prompts.py",
  "src/notion_bridge/planner/toolcalls.py",
  "src/notion_bridge/planner/loop.py",
  "src/notion_bridge/planner/runtime_tools.py",
  "src/notion_bridge/api/responses.py",
  "src/notion_bridge/api/anthropic.py",
  "src/notion_bridge/api/chat.py",
  "src/notion_bridge/api/operations.py",
  // Node services.
  "services/mcp-runtime/src/server.js",
  "services/mcp-runtime/src/tools.js",
  "services/mcp-runtime/src/paths.js",
  "services/mcp-runtime/src/platform.js",
  "services/notion-private-mcp/src/server.js",
  "services/notion-private-mcp/run-from-account.js",
  // Shared configuration and tooling.
  "config/codex-cli-config.toml",
  "config/codex-models.json",
  "config/opencode.jsonc",
  "scripts/render-config.mjs",
  "scripts/codex/install-config.mjs",
  "scripts/codex/patch-webview.mjs",
  "scripts/codex/install-model-aliases.mjs",
  "scripts/install/linux.sh",
  "scripts/install/windows.ps1",
  "scripts/checks/check-public-release.mjs",
  "state-template/.notionagents/models.json",
  // Deployment.
  "deploy/systemd/notioncode-bridge.service",
  "deploy/systemd/notioncode-runtime.service",
  "deploy/systemd/notioncode.target",
  "deploy/docker/Dockerfile",
  "deploy/docker/docker-compose.yml",
  // Tests.
  "tests/bridge/test_server_regressions.py",
  "tests/node/render-config.test.mjs",
  "pyproject.toml",
  "Makefile",
];

const forbiddenDuplicates = [
  // Pre-2.0 locations; leaving them behind would mean two copies of one thing.
  "bridge",
  "runtime",
  "notion-private-api-mcp",
  "windows",
  "public-repo",
  "bin/codex",
  "codex-notion.cmd",
  "install.ps1",
  "start.ps1",
  "stop.ps1",
  "status.ps1",
  "verify.ps1",
  "opencode-notion.cmd",
  "config/vscode-remote-settings.json",
  "deploy/systemd/notion-code-mcp.service",
  "deploy/systemd/notion-fable-proxy.service",
];

const errors = [];

for (const relative of requiredShared) {
  if (!fs.existsSync(path.join(root, relative))) {
    errors.push(`missing shared project file: ${relative}`);
  }
}

for (const relative of forbiddenDuplicates) {
  if (fs.existsSync(path.join(root, relative))) {
    errors.push(`superseded path must not exist: ${relative}`);
  }
}

for (const relative of [
  "deploy/systemd/notioncode-bridge.service",
  "deploy/systemd/notioncode-runtime.service",
  "deploy/systemd/notioncode.target",
]) {
  const content = fs.readFileSync(path.join(root, relative), "utf8");
  if (!content.includes("__NOTIONCODE_ROOT__")) {
    errors.push(`portable systemd template is missing __NOTIONCODE_ROOT__: ${relative}`);
  }
  if (relative.endsWith(".service")) {
    for (const placeholder of ["__USER_HOME__", "__SERVICE_USER__"]) {
      if (!content.includes(placeholder)) {
        errors.push(`portable systemd template is missing ${placeholder}: ${relative}`);
      }
    }
    if (!/^Restart=always$/m.test(content)) {
      errors.push(`an always-on unit must set Restart=always: ${relative}`);
    }
  }
  if (content.includes(legacyProjectPath)) {
    errors.push(`systemd template contains a machine-specific path: ${relative}`);
  }
}

for (const relative of ["config/codex-cli-config.toml", "config/opencode.jsonc"]) {
  const content = fs.readFileSync(path.join(root, relative), "utf8");
  if (!content.includes("__NOTIONCODE_ROOT__")) {
    errors.push(`shared config must use __NOTIONCODE_ROOT__: ${relative}`);
  }
  if (/sk-[A-Za-z0-9_-]{12,}/.test(content)) {
    errors.push(`shared config contains a credential: ${relative}`);
  }
}

// Client configs and units must agree on where the services listen.
const codexConfig = fs.readFileSync(path.join(root, "config/codex-cli-config.toml"), "utf8");
const openCodeConfig = fs.readFileSync(path.join(root, "config/opencode.jsonc"), "utf8");
for (const [label, content] of [["Codex", codexConfig], ["OpenCode", openCodeConfig]]) {
  if (!content.includes("http://127.0.0.1:8765/v1")) {
    errors.push(`${label} config does not point at the loopback bridge on 8765`);
  }
}
for (const [label, content] of [["Codex", codexConfig], ["OpenCode", openCodeConfig]]) {
  if (content.includes("services/mcp-runtime")) {
    errors.push(`${label} config must reference the private MCP, not the coding runtime`);
  }
  if (!content.includes("services/notion-private-mcp/run-from-account.js")) {
    errors.push(`${label} config does not reference the private Notion MCP entry point`);
  }
}

if (errors.length) {
  for (const error of errors) console.error(`ERROR: ${error}`);
  process.exit(1);
}

console.log("Unified cross-platform layout is valid.");
