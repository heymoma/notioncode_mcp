import os from "node:os";
import path from "node:path";

export const MIN_SECRET_LENGTH = 24;

/**
 * Validate the runtime environment once, at startup.
 *
 * A service that is meant to stay up must refuse to start on a bad
 * configuration rather than fail per request with an opaque error.
 */
export function loadConfig(env = process.env) {
  const secret = String(env.MCP_PATH_SECRET || "");
  if (secret.length < MIN_SECRET_LENGTH) {
    throw new Error(
      `MCP_PATH_SECRET must be at least ${MIN_SECRET_LENGTH} characters`,
    );
  }
  if (!/^[A-Za-z0-9_-]+$/.test(secret)) {
    throw new Error("MCP_PATH_SECRET must be URL-safe (A-Z, a-z, 0-9, - and _)");
  }
  const port = Number(env.PORT || 8787);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`PORT must be a TCP port number, got ${env.PORT}`);
  }
  const host = String(env.HOST || "127.0.0.1");
  const root = path.resolve(String(env.CODE_ROOT || os.homedir()));
  return {
    secret,
    port,
    host,
    root,
    endpoint: `/mcp/${secret}`,
    maxReadBytes: positiveInt(env.MAX_READ_BYTES, 2_000_000),
    maxWriteBytes: positiveInt(env.MAX_WRITE_BYTES, 8_000_000),
    maxShellOutputBytes: positiveInt(env.MAX_SHELL_OUTPUT_BYTES, 2_000_000),
    shellTimeoutMs: positiveInt(env.SHELL_TIMEOUT_MS, 30_000),
    maxShellTimeoutMs: positiveInt(env.MAX_SHELL_TIMEOUT_MS, 600_000),
  };
}

function positiveInt(value, fallback) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

/**
 * Environment handed to shell commands.
 *
 * The bridge's MCP path secret is the only thing protecting this runtime, and
 * every command the model runs used to inherit it through process.env.
 */
export function childEnvironment(env = process.env) {
  const child = { ...env };
  for (const name of ["MCP_PATH_SECRET", "NOTION_TOKEN_V2", "NOTION_MCP_RUNTIME_URL"]) {
    delete child[name];
  }
  return child;
}
