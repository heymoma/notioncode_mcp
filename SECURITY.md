# Security policy

This project uses an unofficial Notion private API and authenticates with the
`token_v2` browser session cookie. That cookie grants the same access as the
signed-in Notion account and must be treated like a password.

## Threat model

The bridge is a **local** service. It does not authenticate requests: anything
that can reach `127.0.0.1:8765` can spend your Notion sessions, and anything that
can reach `127.0.0.1:8787` with the path secret can read, write and execute
inside `CODE_ROOT`. Both services therefore bind to loopback only.

Binding `NOTION_BRIDGE_HOST` to a public address, or publishing the ports, means
handing those capabilities to the network. Do it only behind a trusted reverse
proxy that terminates authentication, and never for the coding runtime.

## Never publish or share

- `token_v2`, full browser cookies, account JSON files or screenshots of them;
- `.runtime/env/bridge.env`, `.runtime/env/mcp-runtime.env` or `MCP_PATH_SECRET`;
- `~/.notionagents/`, `.runtime/`, logs or Codex config backups.

Use `notion-agent init --token-v2 -` so the token is read from standard input
instead of appearing in shell history or the process list.

Before publishing, run:

```bash
node scripts/checks/check-public-release.mjs
git status --short
```

If a credential was committed at any point, deleting it in a later commit is not
enough. Revoke or rotate it first, then purge it from Git history.

## What the service does to contain risk

- Both services listen on `127.0.0.1` by default; a non-loopback bind logs a
  warning at startup.
- The coding runtime confines every path to `CODE_ROOT`, resolving symlinks so a
  link pointing outside cannot be used to escape.
- `MCP_PATH_SECRET`, `NOTION_TOKEN_V2` and `NOTION_MCP_RUNTIME_URL` are stripped
  from the environment of shell commands the model runs.
- Credentials and state files are written atomically with mode `600`;
  `~/.notionagents` is `700`.
- Logs contain events, hashed correlation IDs and counters — never prompts, tool
  results, file contents, images or cookies.
- The bridge unit runs with `NoNewPrivileges`, `ProtectSystem=strict` and
  `ProtectHome=read-only`, with write access only to its own state.

The coding runtime is deliberately **not** sandboxed: its purpose is to edit
files and run build and test commands on your behalf. Set `CODE_ROOT` to the
narrowest directory that still lets you work.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting / Security Advisory feature. Do not
include live credentials, cookies, private pages or user data in the report. If
private reporting is unavailable, open an issue containing only a minimal,
redacted description and request a private contact channel.
