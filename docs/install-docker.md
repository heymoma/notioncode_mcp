# Headless-деплой в контейнере

Контейнер обслуживает API-клиентов: Claude Code, OpenCode, свои скрипты. Он **не
подходит** для Codex в VS Code — установщик расширения и `~/.codex/config.toml`
живут на хосте, а не в образе. Для Codex используйте нативную установку.

## Что запускается

Один образ, два процесса под общим entrypoint: bridge на `8765` и coding-runtime
на `8787`. Если любой из них падает, entrypoint завершает контейнер целиком, а
восстановление берёт на себя `restart: unless-stopped` — так две половины не
расходятся по состоянию.

## Запуск

```bash
cd notioncode_mcp
mkdir -p workspace                          # код, который агент сможет менять
docker compose -f deploy/docker/docker-compose.yml up -d --build
docker compose -f deploy/docker/docker-compose.yml ps
curl -fsS http://127.0.0.1:8765/livez | jq .
```

Порты публикуются только на `127.0.0.1`. Bridge не аутентифицирует запросы, так
что выносить их наружу без доверенного прокси нельзя.

## Добавить Notion-сессию

Состояние живёт в томе `notioncode-state`, смонтированном как `/state`:

```bash
docker compose -f deploy/docker/docker-compose.yml exec notioncode \
  notion-agent init --token-v2 - --account /state/.notionagents/notion_account.json
docker compose -f deploy/docker/docker-compose.yml exec notioncode \
  notion-agent doctor --account /state/.notionagents/notion_account.json --json
curl -fsS -X POST http://127.0.0.1:8765/admin/accounts/reload | jq .account_pool
```

Первая команда ждёт stdin: вставьте значение `token_v2`, Enter, `Ctrl-D`.
Дополнительные сессии — в `/state/.notionagents/accounts/account-02.json` и далее.

## Настройка

Переменные окружения задаются в `docker-compose.yml` или в `.env` рядом с ним:

| Переменная | По умолчанию | Значение |
|---|---|---|
| `NOTIONCODE_WORKSPACE` | `./workspace` | каталог хоста, монтируемый в `/workspace` |
| `NOTION_FORCE_MODEL` | не задано | принудительная модель |
| `NOTION_LOG_LEVEL` | `INFO` | уровень логирования |
| `NOTION_INFERENCE_TIMEOUT_SECONDS` | `180` | таймаут одного inference |

Полный список — в [`configuration.md`](configuration.md). `MCP_PATH_SECRET`
генерируется при первом старте и сохраняется в `/state/.notionagents/mcp-path-secret`.

Внутри контейнера `NOTION_BRIDGE_HOST=0.0.0.0` — иначе публикация порта не
работала бы. Изоляция обеспечивается тем, что порт биндится на loopback хоста.

## Клиенты

```bash
# Claude Code
export ANTHROPIC_BASE_URL=http://127.0.0.1:8765
export ANTHROPIC_AUTH_TOKEN=local-notion-bridge

# Любой OpenAI-совместимый клиент
curl -fsS http://127.0.0.1:8765/v1/models | jq '.data[].id'
```

Подробнее — [`clients.md`](clients.md).

## Эксплуатация

```bash
docker compose -f deploy/docker/docker-compose.yml logs -f --tail 100
docker compose -f deploy/docker/docker-compose.yml restart
docker compose -f deploy/docker/docker-compose.yml down
curl -fsS http://127.0.0.1:8765/metrics
```

Healthcheck образа опрашивает `/livez` каждые 30 секунд. Логи ограничены
`max-size: 20m`, `max-file: 5` — контейнер не заполнит диск.

## Бэкап

```bash
docker run --rm -v notioncode-state:/state -v "$PWD:/backup" alpine \
  tar -czf /backup/notioncode-state-$(date +%F).tar.gz -C / state
```

Архив содержит cookies Notion — храните его как пароль.

## Обновление

```bash
git pull --ff-only
docker compose -f deploy/docker/docker-compose.yml up -d --build
```

Том состояния сохраняется, сессии Notion пересоздавать не нужно.
