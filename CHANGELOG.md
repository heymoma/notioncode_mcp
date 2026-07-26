# Changelog

## 2.0.0 — реструктуризация под 24/7-сервис

Крупное обновление структуры, надёжности и эксплуатации. Поведение для клиентов
сохранено: Codex, OpenCode и Claude Code работают как раньше, но сервис стал
пригоден для непрерывной работы без присмотра.

### Структура

- Bridge стал Python-пакетом `src/notion_bridge/`: один файл на 2315 строк
  разбит на `settings`, `service`, `app`, `accounts/`, `state/`, `notion/`,
  `planner/`, `api/`, `metrics`, `sd_notify`. `PYTHONPATH`-хак больше не нужен —
  пакет устанавливается через `pyproject.toml`.
- `runtime/` → `services/mcp-runtime/` с разделением на `config`, `paths`,
  `platform`, `tools`, `server`; `notion-private-api-mcp/` →
  `services/notion-private-mcp/`.
- Тесты вынесены в `tests/bridge/` и `tests/node/`; установочные скрипты — в
  `scripts/install/`, `scripts/windows/`, `scripts/codex/`, `scripts/checks/`,
  `scripts/dev/`.
- Windows получил единый вход `notioncode.ps1` (`install`, `start`, `stop`,
  `restart`, `status`, `verify`, `watch`, `logs`) вместо пяти скриптов в корне.
- Добавлены `Makefile` и `pyproject.toml` с конфигурацией ruff и pytest.

### Эксплуатация

- Разделены проверки состояния: `/livez` (процесс жив), `/readyz` (есть
  свободная Notion-сессия, иначе `503` + `Retry-After`) и `/healthz` (полное
  состояние). Coding-runtime получил собственный `/healthz`.
- `/metrics` в формате Prometheus без внешних зависимостей: запросы, латентность,
  токены, состояние аккаунтов, failover, circuit breaker, tool-вызовы.
- `POST /admin/accounts/reload` подхватывает новые и обновлённые Notion-сессии
  без перезапуска; отвечает `409`, если сессия занята.
- systemd: юниты переименованы в `notioncode-bridge.service` и
  `notioncode-runtime.service`, добавлен `notioncode.target`. `Restart=always`,
  без rate limit, watchdog через `sd_notify`, hardening для bridge, лимиты памяти
  и задач. Установщик удаляет устаревшие юниты.
- Graceful shutdown: по SIGTERM активные turns дорабатываются до 30 секунд.
- Добавлен headless-деплой в Docker (`deploy/docker/`) и logrotate-конфиг для
  запуска без systemd. Windows-логи ротируются в `start.ps1`.
- Вся конфигурация объявлена и валидируется в `settings.py`;
  `python -m notion_bridge --check` проверяет окружение без запуска.
- Сервисы получили раздельные env-файлы `.runtime/env/bridge.env` и
  `.runtime/env/mcp-runtime.env` вместо общего `runtime/.env`.

### Исправления

- Утечка памяти: `TurnAffinityStore` создавал `asyncio.Lock` на каждый turn и
  никогда не удалял его для turn'ов, не дошедших до записи. Плюс добавлен предел
  числа записей.
- Утечка аккаунта: при разрыве соединения в стриминге Chat Completions задача
  inference не отменялась дожидаясь завершения, и Notion-сессия оставалась
  занятой до таймаута.
- Секрет `MCP_PATH_SECRET` попадал в окружение любой команды, запущенной
  моделью через `run_shell`. Теперь вырезается вместе с `NOTION_TOKEN_V2`.
- Обход `CODE_ROOT`: симлинк внутри корня, ведущий наружу, проходил проверку
  префикса. Теперь пути резолвятся через реальную ФС.
- `read_file` читал файл целиком в память и только потом сравнивал с
  `max_bytes`; теперь размер проверяется до чтения.
- `/v1/messages` и `/v1/chat/completions` отвечали `502` там, где Responses
  отвечал `503` с `Retry-After`. Политика статусов теперь общая для трёх API,
  добавлен `504` на таймаут.
- Больше 10 account-файлов приводили к `RuntimeError` при старте и
  бесконечному циклу перезапусков. Лишние аккаунты игнорируются с
  предупреждением и полем `ignored_surplus` в `/healthz`.
- Ошибка записи `conversation-state.json` роняла запрос; теперь состояние
  деградирует до нового Notion-треда, а счётчик ошибок виден в `/healthz`.
- Блокирующая запись состояния пула и бесед выполнялась в event loop на каждом
  запросе — вынесена в поток.
- Endpoint coding-runtime перечитывался с диска и переустанавливал MCP-сессию на
  каждый tool-вызов; теперь резолвится один раз и использует общий пул соединений.
- `verify.ps1` читал несуществующие поля каталога моделей
  (`supportedReasoningEfforts`/`reasoningEffort` вместо
  `supported_reasoning_levels`/`effort`) и падал всегда.
- Windows не устанавливал npm-зависимости провайдера OpenCode, а профиль
  OpenCode лежал в `state/opencode` против `.runtime/opencode` на Linux. Пути
  унифицированы.
- Лямбды в planner-циклах захватывали переменные цикла; аргументы теперь
  связываются в момент создания вызова.
- `/healthz` без пула возвращал структуру другой формы, чем с пулом.
- Удалён мёртвый код в стриминге Responses и дублирование разбора tool-вызовов в
  пяти местах.

### Документация

- README сокращён и переориентирован на задачи; подробности вынесены в `docs/`:
  архитектура, конфигурация, эксплуатация 24/7, наблюдаемость, установка
  (Linux/Windows/Docker), клиенты, диагностика, протокол для ИИ-агента,
  разработка.
- CI расширен: ruff, проверка конфигурации, тесты coding-runtime, валидация
  compose и systemd-юнитов.

## Предыдущие версии

### Added

- Opus 5 (`opus-5` / Notion `agave-flan`) в bridge API, Codex model catalog и
  model picker официального VS Code extension.
- Совместимость истории Codex между `openai` и `notion-ai` providers без
  изменения или миграции сохранённых тредов.
- Восстановление JSON, Anthropic `invoke` и hybrid `antml:parameter` tool calls,
  включая ограниченную автоматическую коррекцию malformed-вызовов.
- Кроссплатформенный idempotent installer patch для версий `openai.chatgpt`,
  которые скрывают неизвестные transport model IDs.
- Responses SSE reasoning events для доступных Notion thinking deltas, а также
  немедленный progress и heartbeat каждые 10 секунд для длинных inference.

### Changed

- Linux bridge принудительно направляет все model IDs в Opus 5; legacy IDs
  сохранены только для возобновления старых Codex-тредов.
- Notion получает `modelFromUser=true` на каждом turn, чтобы continuation не
  возвращался в Auto model.
- Переключение модели в существующей Codex conversation начинает новый Notion
  thread, чтобы фактически применялась выбранная модель.
- Codex context window увеличено до 210 000 токенов, а auto-compaction trigger
  установлен на 200 000 total tokens для всех моделей и `defaultModel`.
- Notion inference ограничен настраиваемым timeout; значение по умолчанию
  `NOTION_INFERENCE_TIMEOUT_SECONDS=180` освобождает account lease при зависании.

### Security

- Account JSON, cookies, runtime state и `.env` остаются локальными и исключены
  из public-release artifact и Git tracking.
