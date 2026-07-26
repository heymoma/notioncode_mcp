# Разработка

## Структура репозитория

```text
src/notion_bridge/          Python-сервис (API, пул, планировщик, метрики)
services/mcp-runtime/       Node: coding-tools MCP (файлы и shell в CODE_ROOT)
services/notion-private-mcp/ Node: вендоренный Notion private API MCP
config/                     шаблоны конфигурации клиентов (с __NOTIONCODE_ROOT__)
deploy/systemd/             юниты и target
deploy/docker/              Dockerfile, compose, entrypoint
deploy/logrotate/           ротация для запуска без systemd
scripts/install/            установщики linux.sh и windows.ps1
scripts/windows/            команды Windows, вызываются из notioncode.ps1
scripts/codex/              генерация ~/.codex/config.toml и патч расширения
scripts/checks/             аудит структуры и подготовки к публикации
scripts/dev/                запуск сервисов в foreground
tests/bridge/               тесты Python
tests/node/                 тесты установочных скриптов
state-template/             эталонные model aliases
docs/                       документация
```

`notioncode.ps1` в корне — единственная точка входа для Windows; `Makefile` — для
Linux и разработки.

## Быстрый старт

```bash
make deps        # venv, pip install -e ., npm ci для обоих Node-сервисов
make check       # всё, что гоняет CI
make help        # список команд
```

## Проверки

```bash
make test-python     # unittest discover -s tests/bridge -t .
make test-node       # тесты coding-runtime и установочных скриптов
make lint            # ruff + node --check + bash -n
node scripts/checks/check-layout.mjs
node scripts/checks/check-public-release.mjs
.runtime/notion-agent-cli-venv/bin/python -m notion_bridge --check
```

Контрактные проверки официального Codex app-server требуют установленного
расширения `openai.chatgpt`:

```bash
node tests/node/codex-app-server.mjs
CODEX_TEST_TOOL_LOOP=1 node tests/node/codex-app-server.mjs
CODEX_TEST_CUSTOM_LOOP=1 node tests/node/codex-app-server.mjs
```

## Локальный запуск

```bash
make run-runtime     # терминал 1
make run-bridge      # терминал 2
curl -fsS http://127.0.0.1:8765/healthz | jq .
```

Оба скрипта читают `.runtime/env/*.env`, созданные установщиком, поэтому окружение
совпадает с продакшеном. Без установщика задайте `MCP_PATH_SECRET`,
`NOTION_AGENT_HOME` и `CODE_ROOT` вручную.

## Как добавить возможность

| Что | Куда |
|---|---|
| новая настройка | `settings.py` — объявить, провалидировать, добавить в `summary()` и в `docs/configuration.md` |
| новый endpoint | `api/<область>.py` + router в `app.py` |
| новый формат tool-вызова модели | `planner/toolcalls.py` + тест на реальном тексте |
| изменение промпта | `planner/prompts.py` |
| новая метрика | `metrics.py` — константа имени и хелпер записи |
| политика ошибок | `api/errors.py`, чтобы все три API вели себя одинаково |

Тесты обязательны для изменений в выборе аккаунта, привязке бесед, компакции и
обработке изображений: именно эти пути предотвращают дублирование Notion-тредов и
502-ответы.

## Стиль

Python: ruff, конфигурация в `pyproject.toml`, строка до 96 символов.
JavaScript: без сборки, ESM, двойные кавычки, `node --check` вместо линтера.
Комментарии объясняют, почему код такой, а не пересказывают его.

## Инварианты, которые проверяет CI

- одна реализация на обе ОС; платформенные только установщики и запуск процессов
  (`scripts/checks/check-layout.mjs`);
- юниты остаются переносимыми: `__NOTIONCODE_ROOT__`, `__USER_HOME__`,
  `__SERVICE_USER__`, `Restart=always`;
- в трекинге нет `.env`, account JSON, состояния и секретов
  (`scripts/checks/check-public-release.mjs`);
- конфигурации клиентов ссылаются на loopback-порт 8765 и на актуальный путь
  private-MCP.

## Релиз

1. `make check`
2. Обновить `CHANGELOG.md`
3. Проверить, что документация соответствует поведению
4. `node scripts/checks/check-public-release.mjs`
5. Публикация — [`PUBLISHING.md`](PUBLISHING.md)
