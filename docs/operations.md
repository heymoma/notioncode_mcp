# Эксплуатация 24/7

Runbook для сервиса, который должен работать без присмотра.

## Модель работы

Два systemd-юнита плюс target как общая ручка:

```text
notioncode.target
├── notioncode-runtime.service   127.0.0.1:8787   файлы и shell в CODE_ROOT
└── notioncode-bridge.service    127.0.0.1:8765   API + пул Notion-сессий
```

Оба юнита используют `Restart=always`, `RestartSec=2` и
`StartLimitIntervalSec=0`: сервис не должен «сдаваться» после серии падений.
Bridge дополнительно отвечает на systemd watchdog, поэтому перезапускается и
тогда, когда процесс жив, но перестал обрабатывать запросы.

## Ежедневные команды

```bash
systemctl status notioncode.target
sudo systemctl restart notioncode.target
sudo systemctl restart notioncode-bridge.service     # только API
journalctl -fu notioncode-bridge.service -o cat      # живые логи
journalctl -u notioncode-bridge.service --since "1 hour ago" -o cat | jq .
curl -fsS http://127.0.0.1:8765/healthz | jq .
curl -fsS http://127.0.0.1:8765/readyz  | jq .
curl -fsS http://127.0.0.1:8787/healthz | jq .
make help
```

## Проверки состояния

| Endpoint | Код | Что означает |
|---|---|---|
| `/livez` | 200 | процесс отвечает; на это реагирует supervisor |
| `/readyz` | 200 / 503 | есть свободная Notion-сессия; при 503 в теле `reason`, в заголовках `Retry-After` |
| `/healthz` | 200 | полное состояние: пул, аккаунты, состояние тредов, настройки |
| `/metrics` | 200 | Prometheus-метрики |

Правильное разделение: перезапускать по `/livez`, а отправлять клиентов «подожди»
по `/readyz`. Пустой пул — это не повод для рестарта, это повод добавить аккаунт.

```bash
# Есть ли вообще свободная сессия
curl -fsS http://127.0.0.1:8765/healthz | jq '.account_pool | {configured, available, cooldown, disabled}'

# Что не так с конкретными аккаунтами
curl -fsS http://127.0.0.1:8765/healthz | jq '.account_pool.accounts'
```

## Мониторинг и алерты

Метрики отдаются в текстовом формате Prometheus по `/metrics`; scrape-конфиг и
список метрик — в [`observability.md`](observability.md).

Минимальный набор алертов:

| Условие | Смысл | Действие |
|---|---|---|
| `/livez` не отвечает 2 минуты | процесс мёртв или завис | systemd перезапустит сам; если нет — смотреть журнал |
| `/readyz` даёт 503 дольше 10 минут | все сессии в cooldown или отключены | проверить `last_error` по аккаунтам, обновить `token_v2` |
| `notion_bridge_accounts{state="disabled"} > 0` | сессия отвергнута Notion | пересоздать эту сессию |
| рост `notion_bridge_circuit_breaker_opened_total` | Notion массово отказывает | подождать; проверить статус Notion |
| `notion_bridge_requests_total{status="5xx"}` растёт | ошибки клиентам | сопоставить с `api_request_failed` в журнале |
| p95 `notion_bridge_inference_duration_seconds` близко к таймауту | Notion деградировал | поднять `NOTION_INFERENCE_TIMEOUT_SECONDS` или снизить нагрузку |

## Логи

Один JSON-объект на строку, логгеры `uvicorn.error.notion_bridge` и
`uvicorn.error.notion_pool`. В логи попадают события, короткие correlation ID
(хэши), номера аккаунтов, длительности и коды ошибок. Не попадают: тексты
промптов, tool-результаты, cookies, изображения.

```bash
# Только failover и cooldown за сутки
journalctl -u notioncode-bridge.service --since today -o cat \
  | jq -c 'select(.event | test("failover|cooling|circuit|disabled"))'

# Медленные turns
journalctl -u notioncode-bridge.service --since today -o cat \
  | jq -c 'select(.event == "request_finished" and .duration_ms > 60000)'
```

Ротацию на Linux делает journald. Ограничить объём:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=500M\nMaxRetentionSec=14day\n' \
  | sudo tee /etc/systemd/journald.conf.d/notioncode.conf
sudo systemctl restart systemd-journald
```

Если сервисы запускаются вручную (`scripts/dev/*.sh`) и пишут в
`.runtime/logs/`, поставьте `deploy/logrotate/notioncode_mcp`. На Windows
`scripts/windows/start.ps1` ротирует логи сам при превышении 20 MB.

## Обслуживание аккаунтов

Добавить сессию без перезапуска:

```bash
sudo -u "$USER" -H ./.runtime/notion-agent-cli-venv/bin/notion-agent \
  init --token-v2 - --account "$HOME/.notionagents/accounts/account-03.json"
sudo -u "$USER" -H ./.runtime/notion-agent-cli-venv/bin/notion-agent \
  doctor --account "$HOME/.notionagents/accounts/account-03.json" --json
curl -fsS -X POST http://127.0.0.1:8765/admin/accounts/reload | jq .account_pool
```

Reload вернёт `409`, если какая-то сессия прямо сейчас занята. Это не ошибка:
повторите запрос через несколько секунд. Reload сбрасывает cooldown-состояние
только для аккаунтов, чей файл изменился, — остальные сохраняют историю.

Заменить истёкший `token_v2`: повторить `init` для того же пути, затем `doctor`,
затем reload.

## Обновление

```bash
git pull --ff-only
sudo -H ./scripts/install/linux.sh
```

Установщик идемпотентен: сохраняет существующие credentials, переносит секрет
`MCP_PATH_SECRET` из установки до 2.0, удаляет устаревшие юниты
`notion-code-mcp.service` и `notion-fable-proxy.service` и перезапускает сервисы.
После обновления расширения `openai.chatgpt` запустите установщик снова и
выполните в VS Code `Developer: Reload Window`.

Windows: `git pull --ff-only`, затем `.\notioncode.ps1 install`.

## Бэкап и восстановление

Что стоит сохранять (всё в `~/.notionagents`, режим `700`):

| Файл | Нужен для |
|---|---|
| `notion_account.json`, `accounts/*.json` | сами сессии Notion — единственное, что нельзя восстановить установщиком |
| `models.json` | соответствие моделей внутренним именам Notion |
| `conversation-state.json` | продолжение существующих Codex-бесед в тех же Notion-тредах |
| `pool-state.json` | статистика и cooldown аккаунтов |

```bash
sudo systemctl stop notioncode.target
tar -czf ~/notionagents-backup-$(date +%F).tar.gz -C "$HOME" .notionagents
sudo systemctl start notioncode.target
```

Архив содержит cookies Notion — храните его как пароль.

Восстановление: распакуйте архив в `$HOME`, проверьте `chmod 700
~/.notionagents`, запустите установщик и `notion-agent doctor`.

Потеря `conversation-state.json` или `pool-state.json` не ломает сервис: беседы
просто начнут новые Notion-треды.

## Инциденты

### Все запросы получают 503

```bash
curl -fsS http://127.0.0.1:8765/readyz | jq .
```

- `configured: 0` — нет валидных account-файлов. Проверьте путь и `doctor`,
  затем reload.
- `available: 0`, `cooldown > 0` — Notion временно отклоняет запросы. Дождитесь
  `retry_after`.
- `disabled > 0` — сессия отвергнута (`AUTH_INVALID`/`PREMIUM_REQUIRED`).
  Пересоздайте её.

### Сервис перезапускается по кругу

```bash
journalctl -u notioncode-bridge.service -n 50 --no-pager
```

Ошибка конфигурации выглядит как `Configuration error: ...` и exit code 2.
Проверьте локально:

```bash
set -a; source .runtime/env/bridge.env; set +a
.runtime/notion-agent-cli-venv/bin/python -m notion_bridge --check
```

### Watchdog перезапускает живой сервис

Значит bridge перестал отвечать на пинги. Проверьте, не был ли процесс на паузе
(`SIGSTOP`, отладчик) и не упирается ли он в `MemoryMax=1G`:

```bash
systemctl show notioncode-bridge.service -p MemoryCurrent,MemoryMax,NRestarts
```

При необходимости watchdog можно выключить: `NOTION_WATCHDOG_ENABLED=0` в
`bridge.env` — но тогда зависшее состояние придётся замечать вручную.

### Порт занят

```bash
ss -ltnp | grep -E '8765|8787'
```

Не убивайте незнакомый процесс: смените порт в `.runtime/env/*.env` и
`config/codex-cli-config.toml`, затем перезапустите установщик.

### Диск заполнен

Состояние маленькое; растут обычно журнал и `node_modules`. Ограничьте journald
(выше) и проверьте `du -sh .runtime`.

## Плановая остановка

```bash
sudo systemctl stop notioncode.target
```

Bridge получает SIGTERM, ждёт до 30 секунд завершения активных turns и только
потом закрывает соединения; `TimeoutStopSec=45` оставляет запас. Не используйте
`kill -9` — активный turn потеряет привязку к Notion-треду.
