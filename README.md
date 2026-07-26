# notioncode_mcp

Локальный сервис, который отдаёт Notion AI через OpenAI- и Anthropic-совместимый
API. Codex в VS Code, Codex CLI, OpenCode и Claude Code работают штатно — треды,
turns, approvals, sandbox, tools, MCP, изображения и compaction остаются на
стороне клиента, а inference уходит в Notion.

Рассчитан на непрерывную работу: два supervised-сервиса, health/readiness,
Prometheus-метрики, watchdog, пул до 10 Notion-сессий с балансировкой,
failover и circuit breaker.

> [!WARNING]
> Это неофициальная интеграция с private API Notion. Она использует браузерную
> cookie `token_v2`, равную по чувствительности паролю. Проверьте правила Notion
> и используйте проект на свой риск. Сервисы по умолчанию слушают только
> `127.0.0.1`.

## Обновления и другие проекты

Новости `notioncode_mcp`, обновления и другой софт автора публикуются в
Telegram-канале [«AI головного мозга»](https://t.me/AI_golovnogo_mozga).

## Что внутри

```text
Codex VS Code / Codex CLI / OpenCode / Claude Code
                         │  Responses · Chat Completions · Anthropic Messages
                         ▼
   notioncode-bridge     127.0.0.1:8765        src/notion_bridge/
   ├─ пул Notion-сессий: балансировка, affinity, failover, circuit breaker
   ├─ привязка Codex-треда к Notion-треду (инкрементальные turns)
   └─ health · readiness · /metrics · hot-reload аккаунтов
                         │  notion-agent-cli + локальный account JSON
                         ▼
   Notion AI             fable-5 · gpt-5.6-sol · opus-5
                         │  one-action planner loop (OpenCode / Chat API)
                         ▼
   notioncode-runtime    127.0.0.1:8787        services/mcp-runtime/
   list_files · read_file · write_file · edit_file · run_shell (внутри CODE_ROOT)
```

| Модель в интерфейсе | Bridge/API ID | Codex transport ID | Внутреннее имя Notion |
|---|---|---|---|
| Fable 5 (Notion) | `fable-5` | `gpt-5.5` | `acai-budino-high` |
| GPT-5.6 Sol (Notion) | `gpt-5.6-sol` | `gpt-5.6-sol` | `orange-mousse` |
| Opus 5 (Notion), по умолчанию | `opus-5` | `opus-5` | `agave-flan` |

Linux-установка по умолчанию ставит `NOTION_FORCE_MODEL=opus-5`: старые IDs
продолжают открывать сохранённые Codex-треды, но inference всегда выполняет
Opus 5. Значение меняется в `.runtime/env/bridge.env`.

## Возможности

- официальный `openai.chatgpt` в VS Code без подмены бинарника Codex;
- OpenAI Responses, Chat Completions и Anthropic Messages в одном процессе;
- нативные function/custom tools, `apply_patch`, shell, планы, skills и MCP;
- потоковый Notion thinking и heartbeat в reasoning-панели Codex;
- PNG, JPEG, GIF и WebP как нативные вложения Notion;
- до 10 независимых Notion-сессий с persistent-балансировкой и failover;
- продолжение Codex-сессии в одном Notion-треде без повторной отправки истории;
- штатная compaction на 200 000 токенов и rollover на следующий аккаунт;
- один и тот же код на Linux и Windows — различается только запуск процессов.

## Установка

### Требования

Git · Python 3.10+ · Node.js 18+ и npm · аккаунт Notion с доступным Notion AI.
Для Codex в VS Code — расширение `openai.chatgpt`. Linux дополнительно требует
systemd, `sudo`, `openssl`, `curl`, `getent`, `runuser`. Windows — 10/11 и
PowerShell 5.1+.

### Linux (systemd)

```bash
git clone <GITHUB_REPOSITORY_URL> && cd notioncode_mcp
sudo -H ./scripts/install/linux.sh                     # или CODE_ROOT="$HOME/projects"
```

Добавьте Notion-сессию (токен читается из stdin и не попадает в history):

```bash
sudo -u "$USER" -H ./.runtime/notion-agent-cli-venv/bin/notion-agent \
  init --token-v2 - --account "$HOME/.notionagents/notion_account.json"
sudo -u "$USER" -H ./.runtime/notion-agent-cli-venv/bin/notion-agent \
  doctor --account "$HOME/.notionagents/notion_account.json" --json
sudo -H ./scripts/install/linux.sh                     # включает notion-private MCP
```

Проверка:

```bash
curl -fsS http://127.0.0.1:8765/readyz | jq .
systemctl is-active notioncode-bridge.service notioncode-runtime.service
```

Подробно — [`docs/install-linux.md`](docs/install-linux.md).

### Windows

```powershell
git clone <GITHUB_REPOSITORY_URL>; Set-Location .\notioncode_mcp
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\notioncode.ps1 install
```

Затем `notion-agent init`, `notion-agent doctor`, повторный `install` и
`.\notioncode.ps1 verify`. Для непрерывной работы — `.\notioncode.ps1 watch`.
Подробно — [`docs/install-windows.md`](docs/install-windows.md).

### Docker (headless)

Для API-клиентов без VS Code:

```bash
docker compose -f deploy/docker/docker-compose.yml up -d --build
```

Подробно — [`docs/install-docker.md`](docs/install-docker.md).

## Управление сервисом

```bash
systemctl status notioncode.target          # оба сервиса
sudo systemctl restart notioncode.target
journalctl -fu notioncode-bridge.service -o cat
curl -fsS http://127.0.0.1:8765/healthz | jq .
curl -fsS http://127.0.0.1:8765/metrics
curl -fsS -X POST http://127.0.0.1:8765/admin/accounts/reload | jq .account_pool
make help                                   # остальные команды
```

| Endpoint | Назначение |
|---|---|
| `GET /livez` | процесс отвечает; на это реагирует supervisor |
| `GET /readyz` | есть свободная Notion-сессия; `503` + `Retry-After`, если нет |
| `GET /healthz` | полное состояние: пул, треды, настройки |
| `GET /metrics` | Prometheus: запросы, латентность, токены, failover |
| `POST /admin/accounts/reload` | подхватить новые аккаунты без перезапуска |

Полный runbook — [`docs/operations.md`](docs/operations.md).

## Документация

| Документ | О чём |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | как устроены bridge, планировщик и пул |
| [`docs/configuration.md`](docs/configuration.md) | все переменные окружения и лимиты |
| [`docs/operations.md`](docs/operations.md) | 24/7: мониторинг, алерты, инциденты, бэкап |
| [`docs/observability.md`](docs/observability.md) | метрики, события логов, что алертить |
| [`docs/install-linux.md`](docs/install-linux.md) | установка и аккаунты на Linux |
| [`docs/install-windows.md`](docs/install-windows.md) | установка и аккаунты на Windows |
| [`docs/install-docker.md`](docs/install-docker.md) | headless-деплой в контейнере |
| [`docs/clients.md`](docs/clients.md) | Codex, OpenCode, Claude Code, свои клиенты |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | частые проблемы и диагностика |
| [`docs/ai-agent-protocol.md`](docs/ai-agent-protocol.md) | протокол для ИИ-агента-установщика |
| [`docs/image-inputs.md`](docs/image-inputs.md) | изображения в Codex CLI |
| [`docs/development.md`](docs/development.md) | структура репозитория, тесты, проверки |
| [`docs/PUBLISHING.md`](docs/PUBLISHING.md) | публикация репозитория |

Если установку выполняет ИИ-агент — сначала
[`docs/ai-agent-protocol.md`](docs/ai-agent-protocol.md) и
[`AGENTS.md`](AGENTS.md).

## Лимиты контекста

Codex заявляет окно 210 000 токенов, auto-compaction срабатывает на 200 000
total tokens, вывод tools ограничен 12 000 токенов. Это локальные настройки
клиента, а не реальное окно Notion: увеличение числа в конфиге само по себе не
увеличивает окно модели. Где менять — в
[`docs/configuration.md`](docs/configuration.md#лимиты-контекста-и-токенов).

## Безопасность и лицензия

Перед публикацией прочитайте [`SECURITY.md`](SECURITY.md) и выполните
`node scripts/checks/check-public-release.mjs`. Root-код распространяется по
лицензии MIT; вложенный `services/notion-private-mcp` сохраняет собственный
MIT-файл.
