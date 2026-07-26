# Архитектура

## Принцип

Notion AI — это чат-ассистент, а не agent runtime. Он не умеет вызывать
инструменты, не хранит сессии Codex и не исполняет код. Поэтому bridge не
пытается быть агентом: он представляет модель как **планировщика**, а локальный
Codex/OpenCode — как **оператора**, который исполняет ровно одно рекомендованное
действие и возвращает результат следующим turn'ом.

Это единственный способ сохранить штатное поведение Codex: approvals, sandbox,
apply_patch, MCP и compaction остаются на стороне клиента.

## Процессы

```text
                     ┌──────────────────────────────────────────┐
клиенты ──HTTP──────▶│ notioncode-bridge      127.0.0.1:8765    │
                     │ src/notion_bridge/                       │
                     │                                          │
                     │  api/responses.py    Codex (Responses)   │
                     │  api/chat.py         OpenCode (Chat)     │
                     │  api/anthropic.py    Claude Code         │
                     │  api/operations.py   health · metrics    │
                     │            │                             │
                     │  planner/  │ prompts · toolcalls · loop   │
                     │  state/    │ turn affinity · segments     │
                     │  accounts/ │ пул сессий Notion            │
                     └────────────┼─────────────────────────────┘
                                  │ notion-agent-cli
                                  ▼
                          Notion AI (private API)
                                  │
                     ┌────────────┼─────────────────────────────┐
                     │ notioncode-runtime    127.0.0.1:8787     │
                     │ services/mcp-runtime/                    │
                     │ файлы и shell строго внутри CODE_ROOT    │
                     └──────────────────────────────────────────┘
```

Два процесса разделены не ради красоты: у них разные права. Bridge не должен
трогать проекты пользователя, поэтому в systemd он запускается с
`ProtectHome=read-only` и доступом только к своему состоянию. Runtime, наоборот,
существует чтобы менять файлы и запускать команды, поэтому он не песочница —
его границы это `CODE_ROOT` и неугадываемый секрет в URL.

## Модули bridge

| Модуль | Ответственность |
|---|---|
| `settings.py` | вся конфигурация из окружения, валидация при старте |
| `service.py` | долгоживущие объекты: пул, состояние, MCP-клиент; drain и reload |
| `app.py` | фабрика FastAPI, lifespan, middleware логирования и метрик |
| `accounts/pool.py` | выбор аккаунта, cooldown, failover, circuit breaker, persist |
| `accounts/migrate.py` | миграция account-файлов старого формата |
| `state/turn_affinity.py` | привязка одного Codex turn к аккаунту и Notion-треду |
| `state/conversation_segments.py` | привязка Codex-беседы к сегменту Notion-треда |
| `notion/models.py` | маппинг model IDs, фиксация `modelFromUser` |
| `notion/images.py` | нативные вложения Notion и стриминг thinking |
| `planner/prompts.py` | тексты промптов planner/operator для всех API |
| `planner/toolcalls.py` | разбор tool-вызовов из текста модели |
| `planner/loop.py` | action-loop для Chat API и Notion custom agent |
| `planner/runtime_tools.py` | клиент coding-tools MCP (один pooled httpx-клиент) |
| `api/payloads.py` | построение OpenAI/Anthropic wire-форматов и SSE |
| `api/errors.py` | единая политика статусов: 400 / 503+Retry-After / 504 / 502 |
| `metrics.py` | Prometheus-реестр без внешних зависимостей |
| `sd_notify.py` | READY/WATCHDOG для systemd |

## Как turn попадает в Notion

1. Клиент присылает запрос. Codex дополнительно передаёт `turn_id` и `thread_id`
   в заголовке `x-codex-turn-metadata`.
2. `state/turn_affinity.py` ищет уже начатый turn. Если пришёл повторный запрос с
   тем же fingerprint — отдаётся закэшированный ответ без нового inference.
3. `state/conversation_segments.py` ищет сегмент беседы. Если история клиента
   расширилась append-only, в Notion уходит **только новый хвост**, в тот же
   тред. Если история переписана, модель сменилась или прошла compaction —
   создаётся новый сегмент и берётся следующий аккаунт.
4. `accounts/pool.py` выдаёт аккаунт: сначала предпочтительный (affinity), иначе
   наименее недавно использованный.
5. Prompt собирается в `planner/prompts.py`. Каталог инструментов передаётся
   один раз, в первом turn'е сегмента.
6. Ответ разбирается в `planner/toolcalls.py`: JSON, `<invoke>`-XML или
   гибридный `antml:parameter`. Если модель отказалась («нет доступа к
   файловой системе») или назвала несуществующий инструмент, bridge до трёх раз
   просит исправиться в том же Notion-треде.
7. Результат превращается в `function_call`, `custom_tool_call` или обычное
   сообщение и отдаётся клиенту, при стриминге — как Responses SSE.

## Устойчивость

- **Failover.** Ошибка, которую библиотека считает удалённой, переводит аккаунт в
  cooldown и повторяет запрос на следующем. `AUTH_INVALID` и `PREMIUM_REQUIRED`
  отключают аккаунт до перезапуска или reload.
- **Circuit breaker.** Одинаковая ошибка на трёх аккаунтах за 30 секунд
  открывает общий cooldown, чтобы не выжигать все сессии подряд.
- **Timeout.** Каждый inference ограничен `NOTION_INFERENCE_TIMEOUT_SECONDS`.
  Зависший запрос освобождает аккаунт, а не держит его вечно.
- **Watchdog.** Bridge отправляет systemd `WATCHDOG=1`. Процесс, который жив, но
  не крутит event loop, будет перезапущен.
- **Graceful shutdown.** По SIGTERM lifespan ждёт до 30 секунд, пока
  завершатся активные turns, и только потом закрывает клиентов.
- **Состояние — это оптимизация.** Повреждённый или недоступный для записи файл
  состояния приводит к новому Notion-треду, но никогда не к ошибке запроса.

## Инварианты

1. Одна реализация на обе ОС. Платформенно-специфичны только установщики и
   запуск процессов.
2. Codex остаётся локальным runtime, Notion — только провайдером inference.
3. Сервисы слушают `127.0.0.1`.
4. В логи не попадают промпты, tool-результаты, cookies и изображения — только
   события, короткие correlation ID и счётчики.
