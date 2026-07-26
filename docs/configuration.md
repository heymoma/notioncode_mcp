# Конфигурация

Все настройки читаются из окружения один раз при старте и валидируются в
`src/notion_bridge/settings.py`. Неверное значение — это явная ошибка запуска, а
не загадочный 502 на первом запросе:

```bash
.runtime/notion-agent-cli-venv/bin/python -m notion_bridge --check
```

## Где живёт окружение

| Файл | Кто читает | Создаётся |
|---|---|---|
| `.runtime/env/bridge.env` | `notioncode-bridge.service` | установщиком |
| `.runtime/env/mcp-runtime.env` | `notioncode-runtime.service` | установщиком |

Оба файла имеют режим `600`, принадлежат сервисному пользователю и не попадают в
Git. Разделение осознанное: до 2.0 bridge получал `EnvironmentFile` рантайма и
вместе с ним чужой `PORT`.

После правки:

```bash
sudo systemctl restart notioncode.target
```

## Bridge

### Основное

| Переменная | По умолчанию | Значение |
|---|---|---|
| `NOTION_AGENT_HOME` | `~/.notionagents` | аккаунты, model aliases, состояние |
| `NOTION_BRIDGE_HOST` | `127.0.0.1` | адрес прослушивания |
| `NOTION_BRIDGE_PORT` | `8765` | порт |
| `CODE_ROOT` | `$HOME` | корень, к которому привязаны пути в промптах |
| `NOTION_LOG_LEVEL` | `INFO` | `DEBUG`…`CRITICAL` |
| `NOTIONCODE_ROOT` | каталог репозитория | используется для поиска legacy env |

Значение `NOTION_BRIDGE_HOST`, отличное от loopback, открывает Notion-inference
за пределы машины. Bridge не аутентифицирует запросы, поэтому такой режим
допустим только за доверенным reverse proxy; при старте пишется предупреждение.

### Модели

| Переменная | По умолчанию | Значение |
|---|---|---|
| `NOTION_DEFAULT_MODEL` | `opus-5` | модель, если клиент не указал |
| `NOTION_FORCE_MODEL` | `opus-5` (ставит установщик) | принудительная модель для всех запросов |
| `NOTION_REASONING_EFFORT` | `high` | `low`, `medium`, `high` |
| `NOTION_WORKFLOW_ID` | не задано | ID Notion custom agent вместо planner-протокола |

`NOTION_FORCE_MODEL=` (пустое) возвращает выбор модели клиенту.

### Надёжность

| Переменная | По умолчанию | Значение |
|---|---|---|
| `NOTION_INFERENCE_TIMEOUT_SECONDS` | `180` | лимит одного inference; по истечении аккаунт освобождается |
| `NOTION_MAX_ACCOUNTS` | `10` | сколько сессий использовать (лишние игнорируются) |
| `NOTION_TRANSIENT_COOLDOWN_SECONDS` | `30` | пауза после сетевой ошибки |
| `NOTION_DENIAL_COOLDOWN_SECONDS` | `300` | пауза после отказа Notion |
| `NOTION_CIRCUIT_WINDOW_SECONDS` | `30` | окно подсчёта одинаковых ошибок |
| `NOTION_CIRCUIT_THRESHOLD` | `3` | на скольких аккаунтах ошибка открывает общий cooldown |

### Состояние

| Переменная | По умолчанию | Значение |
|---|---|---|
| `NOTION_TURN_AFFINITY_TTL_SECONDS` | `7200` | сколько помнить привязку turn'а |
| `NOTION_TURN_AFFINITY_MAX_ENTRIES` | `512` | предел записей в памяти |
| `NOTION_CONVERSATION_TTL_SECONDS` | `2592000` | 30 дней на привязку беседы |
| `NOTION_CONVERSATION_MAX_ENTRIES` | `500` | предел записей на диске |

### Planner и coding tools

| Переменная | По умолчанию | Значение |
|---|---|---|
| `NOTION_MCP_RUNTIME_URL` | из `MCP_PATH_SECRET` | endpoint coding-tools MCP |
| `NOTION_MCP_RUNTIME_PORT` | `8787` | порт, если URL собирается из секрета |
| `NOTION_RUNTIME_TOOL_TIMEOUT_SECONDS` | `120` | таймаут одного tool-вызова |
| `NOTION_PLANNER_MAX_STEPS` | `20` | предел действий в Chat-планировщике |
| `NOTION_PLANNER_CORRECTION_ATTEMPTS` | `3` | попыток вернуть модель в протокол |
| `NOTION_REASONING_HEARTBEAT_SECONDS` | `10` | как часто писать «Still working…» |

### Операционные переключатели

| Переменная | По умолчанию | Значение |
|---|---|---|
| `NOTION_METRICS_ENABLED` | `true` | `/metrics`; иначе `404` |
| `NOTION_ADMIN_ENABLED` | `true` | `/admin/accounts/reload`; иначе `404` |
| `NOTION_WATCHDOG_ENABLED` | авто по `NOTIFY_SOCKET` | пинги systemd watchdog |

## Coding-tools runtime

| Переменная | По умолчанию | Значение |
|---|---|---|
| `MCP_PATH_SECRET` | генерируется | секрет в пути URL, минимум 24 URL-safe символа |
| `CODE_ROOT` | `$HOME` | единственный каталог, доступный инструментам |
| `HOST` | `127.0.0.1` | адрес прослушивания |
| `PORT` | `8787` | порт |
| `MAX_READ_BYTES` | `2000000` | верхняя граница `read_file` |
| `MAX_WRITE_BYTES` | `8000000` | верхняя граница `write_file` |
| `MAX_SHELL_OUTPUT_BYTES` | `2000000` | буфер вывода команды |
| `SHELL_TIMEOUT_MS` | `30000` | таймаут команды по умолчанию |
| `MAX_SHELL_TIMEOUT_MS` | `600000` | максимум, который может запросить модель |
| `NOTION_SHELL` | `/bin/bash`, `powershell.exe` | интерпретатор команд |

`MCP_PATH_SECRET`, `NOTION_TOKEN_V2` и `NOTION_MCP_RUNTIME_URL` вырезаются из
окружения дочерних процессов: иначе любая команда, запущенная моделью, могла бы
прочитать секрет, который защищает сам runtime.

## Лимиты контекста и токенов

Это настройки клиента и metadata моделей. Они не отменяют реальные ограничения
Notion AI: увеличение числа в конфиге само по себе не увеличивает окно модели.

| Лимит | Значение | Где менять |
|---|---:|---|
| Заявленное окно Codex | 210 000 | `model_context_window` в `config/codex-cli-config.toml`; `context_window` и `max_context_window` у всех моделей и `defaultModel` в `config/codex-models.json` |
| Порог auto-compaction | 200 000 | `model_auto_compact_token_limit` в `config/codex-cli-config.toml`; `auto_compact_token_limit` в `config/codex-models.json` |
| Область подсчёта | `total` | `model_auto_compact_token_limit_scope` |
| Эффективная доля окна | 100% | `effective_context_window_percent` |
| Truncation каталога | 10 000 | `truncation_policy.limit` |
| Вывод tools | 12 000 | `tool_output_token_limit` |
| Окно OpenCode | 100 000 | `provider.notion-fable.models.*.limit.context` в `config/opencode.jsonc` |
| Output OpenCode | 40 000 | `provider.notion-fable.models.*.limit.output` |

Держите одинаковые значения у всех моделей и у `defaultModel`. Порог
auto-compaction должен остаться ниже эффективного окна: `200 000 < 210 000`.
После правки повторно запустите установщик, выполните в VS Code
`Developer: Reload Window` и начните новый чат.

Bridge не задаёт жёсткий `max_output_tokens` — длину ответа определяет Notion.
`count_tokens` для Anthropic-совместимого endpoint возвращает оценку
`len(JSON) / 4`.

Изображения расходуют контекст динамически; оценка считается в
`_openai_image_tokens()` (`src/notion_bridge/notion/images.py`). Там же жёсткие
границы: 10 изображений на запрос, 20 MiB на изображение, 50 MiB суммарно.

## Аккаунты Notion

```text
~/.notionagents/notion_account.json          основной
~/.notionagents/accounts/account-02.json     дополнительные
...
~/.notionagents/accounts/account-10.json
```

Для каждого повторите `notion-agent init`, меняя только `--account`. Дубликаты по
`token_v2` или Notion-пользователю исключаются автоматически; аккаунты сверх
`NOTION_MAX_ACCOUNTS` игнорируются с предупреждением в логе и полем
`ignored_surplus` в `/healthz`.

Подхватить изменения без перезапуска:

```bash
curl -fsS -X POST http://127.0.0.1:8765/admin/accounts/reload | jq .account_pool
```

Если в этот момент какая-то сессия занята, endpoint ответит `409` — повторите
после завершения turn'а.
