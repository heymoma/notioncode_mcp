# Установка на Linux

Установщик создаёт два systemd-сервиса. Он запускается из любого каталога, но
требует root: сервисы и Codex-конфиг ставятся для пользователя, вызвавшего
`sudo`.

## Требования

- Git, Python 3.10+, Node.js 18+ и npm;
- systemd, `sudo`, `openssl`, `curl`, `getent`, `runuser`;
- аккаунт Notion с доступным Notion AI;
- для Codex в VS Code — расширение `openai.chatgpt`.

## 1. Клонировать репозиторий

```bash
git clone <GITHUB_REPOSITORY_URL>
cd notioncode_mcp
```

## 2. Запустить установщик

```bash
sudo -H ./scripts/install/linux.sh
```

По умолчанию файловые инструменты видят весь домашний каталог. Ограничить:

```bash
sudo -H env CODE_ROOT="$HOME/projects" ./scripts/install/linux.sh
```

Другие переменные, которые понимает установщик:

| Переменная | По умолчанию | Значение |
|---|---|---|
| `CODE_ROOT` | домашний каталог | единственный каталог для файлов и shell |
| `NOTIONCODE_USER` | `$SUDO_USER` | пользователь, от которого работают сервисы |
| `NOTION_BRIDGE_PORT` | `8765` | порт API |
| `NOTION_MCP_RUNTIME_PORT` | `8787` | порт coding-tools MCP |
| `NOTION_FORCE_MODEL` | `opus-5` | принудительная модель (пусто — выбор клиента) |
| `NOTION_LOG_LEVEL` | `INFO` | уровень логирования |

Что делает установщик:

1. создаёт venv в `.runtime/` и ставит pinned-зависимости плюс пакет
   `notion_bridge` в editable-режиме;
2. ставит npm-зависимости обоих Node-сервисов и провайдер-плагины OpenCode;
3. генерирует `.runtime/env/bridge.env` и `.runtime/env/mcp-runtime.env`
   (режим `600`), перенося `MCP_PATH_SECRET` из установки до 2.0;
4. ставит model aliases в `~/.notionagents/models.json` и миграцию аккаунтов;
5. добавляет managed-блок в `~/.codex/config.toml`, сохраняя ваши настройки; без
   локального account-файла `notion-private` MCP остаётся выключенным;
6. idempotent-патчит model picker установленного `openai.chatgpt`, чтобы в списке
   был `Opus 5 (Notion)`;
7. рендерит systemd-юниты под фактический путь, удаляет устаревшие
   `notion-code-mcp.service` и `notion-fable-proxy.service`;
8. запускает сервисы и ждёт ответа `/healthz`.

## 3. Добавить Notion-сессию

Откройте Notion в браузере: DevTools → Application/Storage → Cookies →
`https://www.notion.so`, скопируйте значение `token_v2`.

```bash
sudo -u "$USER" -H "$PWD/.runtime/notion-agent-cli-venv/bin/notion-agent" \
  init --token-v2 - \
  --account "$HOME/.notionagents/notion_account.json"
```

Команда ждёт stdin. Вставьте **только значение** `token_v2`, нажмите Enter,
затем `Ctrl-D`. Токен не попадёт ни в history, ни в список процессов.

Проверьте credential и повторно запустите установщик — только этот повторный
запуск включит `notion-private` MCP:

```bash
sudo -u "$USER" -H "$PWD/.runtime/notion-agent-cli-venv/bin/notion-agent" \
  doctor --account "$HOME/.notionagents/notion_account.json" --json
sudo -H ./scripts/install/linux.sh
```

Если вы вошли как `root`, `$USER` и `$HOME` уже указывают на root — команды
менять не нужно.

## 4. Проверить

```bash
curl -fsS http://127.0.0.1:8765/readyz  | jq .
curl -fsS http://127.0.0.1:8765/healthz | jq '.account_pool'
curl -fsS http://127.0.0.1:8787/healthz | jq .
systemctl is-active notioncode-bridge.service notioncode-runtime.service
```

Успех: `/readyz` отвечает 200, `account_pool.configured` не меньше 1, оба
сервиса `active`.

## 5. Добавить дополнительные аккаунты

До 10 сессий:

```text
~/.notionagents/notion_account.json
~/.notionagents/accounts/account-02.json
...
~/.notionagents/accounts/account-10.json
```

Для каждого повторите `init`, меняя только путь `--account`, затем:

```bash
curl -fsS -X POST http://127.0.0.1:8765/admin/accounts/reload | jq .account_pool
```

Перезапуск сервиса не нужен. Новые Codex-сессии распределяются
round-robin/LRU; все turns одной сессии продолжают закреплённый Notion-тред;
при ошибке аккаунт уходит в cooldown, а запрос повторяется на следующем.

## Codex в VS Code

1. Установите расширение `openai.chatgpt`.
2. Завершите установку и авторизацию Notion выше.
3. Выполните команду `Developer: Reload Window`.
4. Откройте новый Codex-чат и выберите `Opus 5 (Notion)`.

Отдельный `chatgpt.cliExecutable` не нужен: расширение и Codex CLI читают один
`~/.codex/config.toml`. Установщик меняет только блоки между маркерами
`BEGIN/END notioncode_mcp` и делает backup перед изменением. Некоторые версии
расширения скрывают неизвестные transport IDs — установщик патчит этот фильтр
идемпотентно. После обновления расширения запустите установщик снова и выполните
Reload Window.

## Удаление

```bash
sudo systemctl disable --now notioncode.target notioncode-bridge.service notioncode-runtime.service
sudo rm -f /etc/systemd/system/notioncode-{bridge,runtime}.service /etc/systemd/system/notioncode.target
sudo systemctl daemon-reload
```

Удалите managed-блок из `~/.codex/config.toml` (между маркерами
`BEGIN/END notioncode_mcp`). Каталог `~/.notionagents` содержит ваши Notion-сессии
— удаляйте осознанно.
