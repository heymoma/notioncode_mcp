import fs from "node:fs/promises";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import * as z from "zod/v4";

import { childEnvironment } from "./config.js";
import { relativeToRoot, resolveInsideRoot } from "./paths.js";
import { shellInvocation } from "./platform.js";

const execFileAsync = promisify(execFile);

function result(text, extra = {}) {
  return { content: [{ type: "text", text }], ...extra };
}

export function createMcpServer(config) {
  const mcp = new McpServer({ name: "notion-code-runtime", version: "2.0.0" });
  const resolve = (input, options) => resolveInsideRoot(config.root, input, options);

  mcp.registerTool("list_files", {
    title: "List project files",
    description:
      "List files and directories under CODE_ROOT. Paths are relative to CODE_ROOT.",
    inputSchema: { directory: z.string().optional().default(".") },
  }, async ({ directory }) => {
    const dir = resolve(directory, { mustExist: true });
    const entries = await fs.readdir(dir, { withFileTypes: true });
    const lines = entries
      .sort((a, b) => a.name.localeCompare(b.name))
      .map((entry) =>
        `${entry.isDirectory() ? "[dir] " : "      "}` +
        `${relativeToRoot(config.root, path.join(dir, entry.name))}`,
      );
    return result(lines.join("\n") || "(empty)");
  });

  mcp.registerTool("read_file", {
    title: "Read a file",
    description: "Read a UTF-8 text file under CODE_ROOT.",
    inputSchema: {
      file_path: z.string(),
      max_bytes: z.number().int().positive().max(config.maxReadBytes)
        .optional().default(Math.min(500_000, config.maxReadBytes)),
    },
  }, async ({ file_path, max_bytes }) => {
    const file = resolve(file_path, { mustExist: true });
    // Checking the size first keeps a huge file from being loaded into memory
    // only to be rejected afterwards.
    const stat = await fs.stat(file);
    if (!stat.isFile()) throw new Error(`Not a regular file: ${file_path}`);
    if (stat.size > max_bytes) {
      throw new Error(`File exceeds max_bytes (${stat.size} > ${max_bytes}): ${file_path}`);
    }
    return result(await fs.readFile(file, "utf8"));
  });

  mcp.registerTool("write_file", {
    title: "Write a file",
    description:
      "Create or replace a UTF-8 text file under CODE_ROOT. " +
      "Parent directories are created automatically.",
    inputSchema: { file_path: z.string(), content: z.string() },
  }, async ({ file_path, content }) => {
    const bytes = Buffer.byteLength(content);
    if (bytes > config.maxWriteBytes) {
      throw new Error(`Content exceeds ${config.maxWriteBytes} bytes: ${file_path}`);
    }
    const file = resolve(file_path);
    await fs.mkdir(path.dirname(file), { recursive: true });
    await fs.writeFile(file, content, "utf8");
    return result(`Wrote ${relativeToRoot(config.root, file)} (${bytes} bytes).`);
  });

  mcp.registerTool("edit_file", {
    title: "Edit a file",
    description: "Replace an exact text fragment in a UTF-8 file under CODE_ROOT.",
    inputSchema: {
      file_path: z.string(),
      old_text: z.string(),
      new_text: z.string(),
      replace_all: z.boolean().optional().default(false),
    },
  }, async ({ file_path, old_text, new_text, replace_all }) => {
    const file = resolve(file_path, { mustExist: true });
    const current = await fs.readFile(file, "utf8");
    const count = current.split(old_text).length - 1;
    if (!count) throw new Error(`old_text was not found in ${file_path}`);
    if (!replace_all && count !== 1) {
      throw new Error(
        `old_text occurs ${count} times; set replace_all=true or provide a larger fragment`,
      );
    }
    const updated = replace_all
      ? current.split(old_text).join(new_text)
      : current.replace(old_text, new_text);
    await fs.writeFile(file, updated, "utf8");
    return result(
      `Edited ${relativeToRoot(config.root, file)} (${replace_all ? count : 1} replacement).`,
    );
  });

  mcp.registerTool("run_shell", {
    title: "Run shell command",
    description:
      "Run a native shell command on the coding machine. Use cwd relative to " +
      "CODE_ROOT. This can change the machine.",
    inputSchema: {
      command: z.string(),
      cwd: z.string().optional().default("."),
      timeout_ms: z.number().int().positive().max(config.maxShellTimeoutMs)
        .optional().default(config.shellTimeoutMs),
    },
  }, async ({ command, cwd, timeout_ms }) => {
    const workdir = resolve(cwd, { mustExist: true });
    const shell = shellInvocation(command);
    try {
      const { stdout, stderr } = await execFileAsync(shell.executable, shell.args, {
        cwd: workdir,
        timeout: timeout_ms,
        maxBuffer: config.maxShellOutputBytes,
        env: childEnvironment(),
      });
      return result(
        `${stdout}${stderr ? `\n[stderr]\n${stderr}` : ""}` ||
        "(command completed with no output)",
      );
    } catch (error) {
      const stdout = error.stdout || "";
      const stderr = error.stderr || error.message || "";
      return result(`${stdout}${stderr ? `\n[stderr]\n${stderr}` : ""}`, { isError: true });
    }
  });

  return mcp;
}
