# Наблюдаемость

## Метрики

`GET /metrics` отдаёт текстовый формат Prometheus. Реестр реализован в
`src/notion_bridge/metrics.py` без внешних зависимостей, поэтому установка
остаётся одним pinned-требованием.

### Запросы

| Метрика | Тип | Метки |
|---|---|---|
| `notion_bridge_requests_total` | counter | `endpoint`, `status` (`2xx`…`5xx`) |
| `notion_bridge_request_duration_seconds` | histogram | `endpoint` |
| `notion_bridge_requests_in_flight` | counter | `endpoint` (растёт и убывает) |

### Notion inference

| Метрика | Тип | Метки |
|---|---|---|
| `notion_bridge_inference_total` | counter | `model`, `outcome` (`ok`/`error`) |
| `notion_bridge_inference_duration_seconds` | histogram | `model` |
| `notion_bridge_tokens_total` | counter | `model`, `direction` (`input`/`output`) |

### Пул аккаунтов

| Метрика | Тип | Метки |
|---|---|---|
| `notion_bridge_accounts` | gauge | `state` (`ready`, `busy`, `cooldown`, `disabled`) |
| `notion_bridge_account_failovers_total` | counter | — |
| `notion_bridge_circuit_breaker_opened_total` | counter | — |

### Состояние и инструменты

| Метрика | Тип | Метки |
|---|---|---|
| `notion_bridge_conversation_segments` | gauge | — |
| `notion_bridge_turn_affinities` | gauge | — |
| `notion_bridge_runtime_tool_calls_total` | counter | `tool`, `outcome` |
| `notion_bridge_response_cache_hits_total` | counter | — |
| `notion_bridge_planner_corrections_total` | counter | — |
| `notion_bridge_start_time_seconds` | gauge | — |

Гистограммы используют границы, подобранные под Notion: 0.25, 0.5, 1, 2.5, 5,
10, 20, 30, 60, 120, 180, 300 секунд. Секундные бакеты для сервиса, где ответ
занимает минуты, не дали бы ничего полезного.

### Scrape

```yaml
scrape_configs:
  - job_name: notioncode_mcp
    scrape_interval: 30s
    static_configs:
      - targets: ["127.0.0.1:8765"]
```

Полезные выражения:

```promql
# Доля ошибок за 5 минут
sum(rate(notion_bridge_requests_total{status="5xx"}[5m]))
  / sum(rate(notion_bridge_requests_total[5m]))

# p95 длительности inference
histogram_quantile(0.95,
  sum by (le) (rate(notion_bridge_inference_duration_seconds_bucket[15m])))

# Нет ни одной готовой сессии
notion_bridge_accounts{state="ready"} == 0

# Токены в час по моделям
sum by (model) (increase(notion_bridge_tokens_total[1h]))
```

Отключить endpoint: `NOTION_METRICS_ENABLED=0` (тогда `/metrics` даёт `404`).

## События логов

Одна строка — один JSON-объект. Общие поля: `event`, `request_id`, `method`,
`endpoint`, при наличии — `model`, `turn_id`, `conversation_id`, `request_kind`.
`turn_id`, `conversation_id` и `notion_thread_id` — усечённые SHA-256, из них
нельзя восстановить исходные идентификаторы.

### Жизненный цикл

| Событие | Когда | Ключевые поля |
|---|---|---|
| `bridge_started` | старт процесса | `version`, сводка настроек |
| `bridge_stopped` | остановка | `version` |
| `bridge_bound_publicly` | host не loopback | `host` |
| `coding_tools_unconfigured` | нет MCP endpoint | — |
| `account_pool_started` | пул собран | `configured`, `available`, `invalid` |
| `account_pool_reloaded` | после `/admin/accounts/reload` | те же |
| `account_pool_surplus_ignored` | аккаунтов больше лимита | `maximum`, `ignored` |
| `shutdown_drain_timeout` | turns не завершились за 30 с | `timeout_seconds` |

### Запросы

| Событие | Смысл |
|---|---|
| `request_started` / `request_finished` | границы запроса, `status_code`, `duration_ms` |
| `request_details` | число переданных инструментов |
| `model_resolved` | запрошенная и фактическая модель, признак `forced` |
| `responses_context` | `full` или `continuation`, число items, изображения, оценка токенов |
| `turn_affinity_checked` | `new`, `reused`, `model_changed` |
| `conversation_segment_checked` | `new`, `continued`, `rollover` + `rollover_reason` |
| `response_cache_hit` | повторный идентичный turn обслужен без inference |
| `planner_correction_requested` | модель нарушила протокол, просим исправиться |
| `planner_correction_exhausted` | попытки исчерпаны, отдаём как есть |
| `api_request_failed` | ошибка клиенту: `status_code`, `error_code` |

### Аккаунты

| Событие | Смысл |
|---|---|
| `account_selected` | `selection`: `balanced`, `affinity` или `failover` |
| `account_request_succeeded` | `duration_ms`, счётчик успехов |
| `account_request_failed` | `error_code`, `cooldown_seconds`, `disabled` |
| `account_request_timed_out` | превышен `NOTION_INFERENCE_TIMEOUT_SECONDS` |
| `account_request_rejected` | локальная ошибка, failover не нужен |
| `account_failover` | переход на другой аккаунт |
| `account_pool_cooling_down` | все свободные в cooldown, `retry_after` |
| `account_pool_exhausted` | все аккаунты отказали для одной операции |
| `circuit_breaker_opened` / `circuit_breaker_active` | общий cooldown |

### Инструменты

| Событие | Смысл |
|---|---|
| `planner_action_executed` | шаг Chat-планировщика, `tool` |
| `workflow_tool_executed` | шаг Notion custom agent |
| `runtime_tool_transport_failed` | coding-tools MCP недоступен |

## Что не логируется

Промпты, ответы модели, tool-результаты, содержимое файлов, изображения,
`token_v2`, полные cookies, `MCP_PATH_SECRET`. Ключи состояния хэшируются перед
записью на диск и в лог. Если нужен более подробный разбор — поднимите
`NOTION_LOG_LEVEL=DEBUG`: это добавит служебные сообщения библиотек, но не
раскроет содержимое запросов.
