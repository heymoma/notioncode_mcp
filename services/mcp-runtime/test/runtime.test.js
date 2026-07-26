import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { childEnvironment, loadConfig } from "../src/config.js";
import { resolveInsideRoot } from "../src/paths.js";
import { shellInvocation } from "../src/platform.js";

const SECRET = "0123456789abcdef0123456789abcdef";

function sandbox() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "notion-runtime-"));
}

test("linux uses a login bash shell", () => {
  const invocation = shellInvocation("echo hi", { platform: "linux", configuredShell: "" });
  assert.equal(invocation.executable, "/bin/bash");
  assert.deepEqual(invocation.args, ["-lc", "echo hi"]);
});

test("windows uses a non-interactive powershell", () => {
  const invocation = shellInvocation("echo hi", { platform: "win32", configuredShell: "" });
  assert.equal(invocation.executable, "powershell.exe");
  assert.ok(invocation.args.includes("-NonInteractive"));
  assert.equal(invocation.args.at(-1), "echo hi");
});

test("a configured shell overrides the platform default", () => {
  const invocation = shellInvocation("echo hi", {
    platform: "linux",
    configuredShell: "/bin/zsh",
  });
  assert.equal(invocation.executable, "/bin/zsh");
});

test("configuration rejects a short or non-url-safe secret", () => {
  assert.throws(() => loadConfig({ MCP_PATH_SECRET: "too-short" }), /at least 24/);
  assert.throws(
    () => loadConfig({ MCP_PATH_SECRET: `${SECRET}/../etc` }),
    /URL-safe/,
  );
});

test("configuration rejects an invalid port", () => {
  assert.throws(
    () => loadConfig({ MCP_PATH_SECRET: SECRET, PORT: "99999" }),
    /TCP port/,
  );
});

test("shell commands never inherit the MCP path secret", () => {
  const child = childEnvironment({
    MCP_PATH_SECRET: SECRET,
    NOTION_TOKEN_V2: "cookie",
    PATH: "/usr/bin",
  });
  assert.equal(child.MCP_PATH_SECRET, undefined);
  assert.equal(child.NOTION_TOKEN_V2, undefined);
  assert.equal(child.PATH, "/usr/bin");
});

test("paths outside CODE_ROOT are rejected", () => {
  const root = sandbox();
  assert.throws(() => resolveInsideRoot(root, "../outside"), /outside CODE_ROOT/);
  assert.throws(() => resolveInsideRoot(root, "/etc/passwd"), /outside CODE_ROOT/);
  assert.equal(resolveInsideRoot(root, "."), root);
  assert.equal(resolveInsideRoot(root, "nested/file.txt"), path.join(root, "nested/file.txt"));
});

test("a symlink that escapes CODE_ROOT is rejected", () => {
  const root = sandbox();
  const outside = sandbox();
  fs.writeFileSync(path.join(outside, "secret.txt"), "private");
  fs.symlinkSync(outside, path.join(root, "escape"));
  assert.throws(
    () => resolveInsideRoot(root, "escape/secret.txt", { mustExist: true }),
    /outside CODE_ROOT/,
  );
  // A write target inside the escaping directory is checked the same way,
  // through the closest existing ancestor.
  assert.throws(() => resolveInsideRoot(root, "escape/new.txt"), /outside CODE_ROOT/);
});

test("a symlink inside CODE_ROOT still resolves", () => {
  const root = sandbox();
  fs.mkdirSync(path.join(root, "real"));
  fs.writeFileSync(path.join(root, "real/file.txt"), "ok");
  fs.symlinkSync(path.join(root, "real"), path.join(root, "link"));
  assert.equal(
    resolveInsideRoot(root, "link/file.txt", { mustExist: true }),
    path.join(root, "link/file.txt"),
  );
});

test("a missing path is only rejected when it must exist", () => {
  const root = sandbox();
  assert.equal(resolveInsideRoot(root, "new.txt"), path.join(root, "new.txt"));
  assert.throws(
    () => resolveInsideRoot(root, "new.txt", { mustExist: true }),
    /does not exist/,
  );
});
