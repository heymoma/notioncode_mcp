# Клиенты

Bridge отдаёт три совместимых API на одном порту. Возможности различаются, потому
что различается то, что клиент умеет сам.

| Клиент | API | Продолжение Notion-треда | Инструменты | Изображения | Стриминг thinking |
|---|---|---|---|---|---|
| Codex (VS Code, CLI) | Responses | да, инкрементально | нативные Codex | да | да |
| OpenCode | Chat Completions | нет | planner loop в bridge | нет | нет |
| Claude Code | Anthropic Messages | нет | инструменты клиента | нет | нет |
| Свой код | любое из трёх | как у соответствующего API | | | |

## Codex

Основной сценарий. Установщик настраивает `~/.codex/config.toml`, поэтому и
расширение VS Code, и CLI подхватывают провайдер автоматически.

```bash
codex                                   # обычный запуск
codex --image screenshot.png -- "Что здесь не так?"
```

Codex передаёт `turn_id` и `thread_id`, поэтому bridge продолжает один Notion-тред
и отправляет туда только новые события turn'а, а не всю историю. Компакция
(`/v1/responses/compact`) начинает новый сегмент и берёт следующий аккаунт.

Изображения — [`image-inputs.md`](image-inputs.md).

## OpenCode

Профиль лежит в `.runtime/opencode` и не затрагивает ваш глобальный конфиг.

```bash
OPENCODE_CONFIG_DIR="$PWD/.runtime/opencode" opencode
```

На Windows:

```powershell
.\scripts\windows\opencode.cmd
```

OpenCode не даёт bridge собственного agent-runtime, поэтому action-loop крутит сам
bridge: модель возвращает одно действие в JSON, bridge выполняет его через
coding-tools MCP (`list_files`, `read_file`, `write_file`, `edit_file`,
`run_shell`) и передаёт результат следующим turn'ом. Предел шагов —
`NOTION_PLANNER_MAX_STEPS` (по умолчанию 20).

Все пути ограничены `CODE_ROOT`. Симлинк, ведущий за его пределы, отклоняется.

## Claude Code

Шаблон настроек — `config/claude-settings.json`. Объедините его со своими
настройками вручную, не удаляя существующие поля:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8765",
    "ANTHROPIC_AUTH_TOKEN": "local-notion-bridge",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "opus-5"
  }
}
```

Ограничение, о котором стоит знать: Anthropic Messages не передаёт стабильный
идентификатор беседы, поэтому каждый turn начинает новый Notion-тред и отправляет
историю заново. Инструменты работают — их исполняет сам Claude Code, bridge лишь
переводит рекомендации модели в `tool_use`. Изображения на этом endpoint не
загружаются в Notion нативно, они упоминаются как факт.

## Свой клиент

```bash
curl -fsS http://127.0.0.1:8765/v1/models | jq '.data[].id'

curl -fsS http://127.0.0.1:8765/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"opus-5","messages":[{"role":"user","content":"Привет"}]}' | jq -r '.choices[0].message.content'
```

Аутентификации нет: доступ определяется тем, что порт слушает `127.0.0.1`.
Заголовок `Authorization` принимается и игнорируется — он нужен только клиентам,
которые отказываются работать без него.

### Что учитывать при интеграции

- `503` с `Retry-After` означает «все сессии заняты или в cooldown» — уважайте
  заголовок, не ретрайте немедленно.
- `504` — inference превысил `NOTION_INFERENCE_TIMEOUT_SECONDS`.
- Стриминг — обычный SSE, поток заканчивается `data: [DONE]`.
- `POST /v1/messages/count_tokens` возвращает оценку `len(JSON) / 4`, а не точный
  счёт: у Notion нет endpoint'а подсчёта.
- Один и тот же запрос, повторённый в рамках одного turn'а, обслуживается из
  кэша без нового обращения к Notion.

## Notion custom agent

Если задать `NOTION_WORKFLOW_ID`, bridge перестаёт использовать planner-протокол и
разговаривает с вашим Notion-агентом: тот сам эмитит function-JSON, а bridge
исполняет вызовы через coding-tools MCP (до 12 шагов). Режим виден в `/healthz`
как `custom_agent: true`.
