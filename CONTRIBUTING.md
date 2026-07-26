# Contributing

Start with [`docs/development.md`](docs/development.md): it describes the layout,
how to run the services locally and where each kind of change belongs.

## Ground rules

- **One implementation for both platforms.** Shared behaviour lives in
  `src/notion_bridge/`, `services/`, `config/` and `scripts/`; only installers and
  process launch may differ between Linux and Windows. Never add a separate
  platform copy of shared code.
- **Assume the service runs for weeks.** Bound every cache, release every lease
  (including on cancellation), keep blocking I/O off the event loop, and let a
  recoverable failure degrade rather than crash the process.
- **Configuration is declared once.** Add settings to
  `src/notion_bridge/settings.py` with validation, and document them in
  `docs/configuration.md`.
- **Never commit secrets.** No Notion credentials, `.env` files, runtime state,
  logs or generated Codex configuration. Tests must use obviously fake values.

## Before opening a pull request

```bash
make check
```

This runs ruff, both test suites, the configuration validator, the layout audit
and the public-release audit — the same set as CI.

Changes to account selection, conversation affinity, compaction or image
handling require a regression test: those paths are what prevent duplicate
Notion threads and 502 responses.

Update `CHANGELOG.md` and the relevant page under `docs/` in the same change that
alters behaviour.
