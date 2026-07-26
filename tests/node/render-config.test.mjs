#!/usr/bin/env node

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const renderer = path.join(root, "scripts", "render-config.mjs");
const UNITS = [
  "notioncode-runtime.service",
  "notioncode-bridge.service",
  "notioncode.target",
];

function render(name, directory) {
  const destination = path.join(directory, name);
  const result = spawnSync(process.execPath, [
    renderer,
    path.join(root, "deploy", "systemd", name),
    destination,
    "/srv/notioncode",
    "/home/alice",
    "alice",
  ], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  return fs.readFileSync(destination, "utf8");
}

test("renders portable systemd paths and leaves no placeholders", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "notioncode-systemd-"));
  try {
    for (const name of UNITS) {
      const rendered = render(name, directory);
      assert.match(rendered, /\/srv\/notioncode/);
      assert.doesNotMatch(rendered, /__[A-Z0-9_]+__/);
      if (name.endsWith(".service")) {
        assert.match(rendered, /User=alice/);
        assert.match(rendered, /Environment=HOME=\/home\/alice/);
        // Both services must recover on their own, which is the whole point of
        // running them under a supervisor.
        assert.match(rendered, /Restart=always/);
      }
    }
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test("the bridge unit is watchdog-supervised and reads its own env file", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "notioncode-bridge-unit-"));
  try {
    const rendered = render("notioncode-bridge.service", directory);
    assert.match(rendered, /NotifyAccess=main/);
    assert.match(rendered, /WatchdogSec=\d+/);
    assert.match(
      rendered,
      /EnvironmentFile=\/srv\/notioncode\/\.runtime\/env\/bridge\.env/,
    );
    // The bridge must not inherit the coding runtime's environment file.
    assert.doesNotMatch(rendered, /mcp-runtime\.env/);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test("windows-style project roots stay portable", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "notioncode-windows-"));
  try {
    const destination = path.join(directory, "opencode.jsonc");
    const result = spawnSync(process.execPath, [
      renderer,
      path.join(root, "config", "opencode.jsonc"),
      destination,
      "C:\\Users\\alice\\notioncode_mcp",
      "C:\\Users\\alice",
    ], { encoding: "utf8" });
    assert.equal(result.status, 0, result.stderr);
    const rendered = fs.readFileSync(destination, "utf8");
    assert.match(rendered, /C:\/Users\/alice\/notioncode_mcp/);
    assert.doesNotMatch(rendered, /__[A-Z0-9_]+__/);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
