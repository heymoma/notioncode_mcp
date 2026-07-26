# Частые проблемы

Первым делом:

```bash
curl -fsS http://127.0.0.1:8765/readyz | jq .
journalctl -u notioncode-bridge.service -n 30 --no-pager -o cat | jq -c .
```

## `/readyz` показывает `configured: 0`

Bridge не нашёл валидных account-файлов.

```bash
ls -la ~/.notionagents ~/.notionagents/accounts
curl -fsS http://127.0.0.1:8765/healthz | jq '.account_pool.invalid_accounts'
```

Проверьте путь через `notion-agent doctor`, затем перезагрузите пул:

```bash
curl -fsS -X POST http://127.0.0.1:8765/admin/accounts/reload | jq .account_pool
```

Если `NOTION_AGENT_HOME` в `.runtime/env/bridge.env` указывает не туда, исправьте
и перезапустите сервис.

## Аккаунт в состоянии `cooldown`

Это не ошибка установки. Notion временно отклонил запрос, поэтому bridge не
спамит эту сессию и берёт следующую. `retry_after` показывает остаток. Если
сессия падает постоянно — обновите её `token_v2` и повторите `doctor`.

## Аккаунт в состоянии `disabled`

Notion отверг сессию окончательно (`AUTH_INVALID` или `PREMIUM_REQUIRED`).
Пересоздайте её тем же `init` для того же пути и выполните reload.

## `AmbiguousWorkspaceError` при создании аккаунта

У токена доступ к нескольким workspace. Повторите `init`, добавив точное имя из
сообщения об ошибке:

```bash
sudo -u "$USER" -H "$PWD/.runtime/notion-agent-cli-venv/bin/notion-agent" \
  init --token-v2 - --space-name "My Workspace" \
  --account "$HOME/.notionagents/notion_account.json"
```

На Windows добавьте `--space-name "My Workspace"` к той же команде.

## Модели не появились в VS Code

Убедитесь, что `/readyz` отвечает 200, затем выполните `Developer: Reload Window`
и создайте новый чат. Уже открытый app-server может держать конфигурацию,
загруженную до установки. Если после обновления расширения пропал именно Opus —
запустите установщик снова: он восстановит патч model picker без переустановки
`openai.chatgpt`.

## На Windows не переключается модель обратно на Fable 5

Обновите репозиторий, выполните `.\notioncode.ps1 install`, затем
`Developer: Reload Window`. В каталоге Codex Fable использует совместимый ID
`gpt-5.5`, а bridge всегда переводит его в Notion-модель `fable-5`. Создайте
новый чат, чтобы не унаследовать настройки старого треда.

## Модель отвечает подозрительно быстро или заметно хуже

Fable 5, GPT-5.6 Sol и Opus 5 с высоким reasoning обычно не мгновенные. Скорость
сама по себе ничего не доказывает, но если ответы стабильно приходят слишком
быстро и при этом хуже ожидаемого — вероятно, при установке были неверно
прописаны внутренние имена моделей Notion.

Проверьте `friendly_aliases` в `~/.notionagents/models.json`:

```json
{
  "fable-5": "acai-budino-high",
  "gpt-5.6-sol": "orange-mousse",
  "opus-5": "agave-flan"
}
```

```bash
jq '.friendly_aliases' "$HOME/.notionagents/models.json"
```

```powershell
(Get-Content "$HOME\.notionagents\models.json" -Raw | ConvertFrom-Json).friendly_aliases
.\notioncode.ps1 verify
```

Если mapping отличается, не подбирайте имена вручную: обновите репозиторий и
запустите установщик, затем перезапустите сервис и создайте новый чат.

Проверить, какую модель Notion фактически выбрал:

```bash
journalctl -u notioncode-bridge.service --since "10 min ago" -o cat \
  | jq -c 'select(.event == "notion_model_selected")'
```

## Модель отвечает «у меня нет доступа к файловой системе»

Bridge распознаёт такой отказ и до трёх раз просит модель вернуться к протоколу в
том же Notion-треде. Если это происходит постоянно:

```bash
journalctl -u notioncode-bridge.service --since today -o cat \
  | jq -c 'select(.event | startswith("planner_correction"))'
```

Частые причины — очень большой системный промпт клиента и `NOTION_WORKFLOW_ID`,
указывающий на агента, который не умеет вызывать инструменты. Число попыток
настраивается через `NOTION_PLANNER_CORRECTION_ATTEMPTS`.

## Инструменты не работают, `coding_tools.configured: false`

Bridge не знает endpoint coding-runtime.

```bash
grep -c MCP_PATH_SECRET .runtime/env/mcp-runtime.env
grep -c NOTION_MCP_RUNTIME_URL .runtime/env/bridge.env
curl -fsS http://127.0.0.1:8787/healthz | jq .
```

Если файлов нет — запустите установщик. Если runtime не слушает —
`systemctl status notioncode-runtime.service`.

## `Path is outside CODE_ROOT`

Ожидаемое поведение: инструменты видят только `CODE_ROOT`, и симлинк, ведущий за
его пределы, отклоняется. Чтобы расширить область, поменяйте `CODE_ROOT` в
`.runtime/env/mcp-runtime.env` и `.runtime/env/bridge.env` и перезапустите
сервисы — или запустите установщик с нужным `CODE_ROOT`.

## Ответы обрываются на длинных задачах

Проверьте, не срабатывает ли таймаут:

```bash
journalctl -u notioncode-bridge.service --since today -o cat \
  | jq -c 'select(.event == "account_request_timed_out")'
```

Поднимите `NOTION_INFERENCE_TIMEOUT_SECONDS` в `.runtime/env/bridge.env` и
перезапустите сервис. Клиент при таймаусе получает `504`.

## Порт 8765 или 8787 занят

Не запускайте второй экземпляр. Найдите процесс:

```bash
ss -ltnp | grep -E '8765|8787'
```

```powershell
Get-NetTCPConnection -LocalPort 8765,8787 | Select-Object LocalPort,OwningProcess
```

Не завершайте незнакомый процесс без подтверждения: смените порт в
`.runtime/env/*.env` и `config/codex-cli-config.toml`, затем перезапустите
установщик.

## Сервис перезапускается по кругу

```bash
journalctl -u notioncode-bridge.service -n 50 --no-pager
systemctl show notioncode-bridge.service -p NRestarts
```

`Configuration error: ...` и exit code 2 означают неверное значение в
`bridge.env`. Проверьте локально:

```bash
set -a; source .runtime/env/bridge.env; set +a
.runtime/notion-agent-cli-venv/bin/python -m notion_bridge --check
```

## `/admin/accounts/reload` отвечает 409

Какая-то Notion-сессия сейчас обслуживает turn. Это защита от подмены пула под
активным запросом — повторите через несколько секунд.

## Каждый turn начинает новый Notion-тред

Ожидаемо для Claude Code: Anthropic Messages не передаёт идентификатор беседы.
Для Codex это признак проблемы:

```bash
journalctl -u notioncode-bridge.service --since "10 min ago" -o cat \
  | jq -c 'select(.event == "conversation_segment_checked") | {state, rollover_reason}'
```

`history_rewritten` означает, что клиент переписал историю (например, после
`/clear`), `model_changed` — смену модели, `post_compaction` — штатный rollover
после компакции. Постоянный `new` при отсутствии этих причин — повод проверить,
доступен ли для записи `~/.notionagents/conversation-state.json`:

```bash
curl -fsS http://127.0.0.1:8765/healthz | jq '.conversation_segments'
```

Ненулевой `write_errors` указывает на права или заполненный диск.
