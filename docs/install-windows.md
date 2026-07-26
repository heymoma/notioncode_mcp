# Установка на Windows

Windows 10/11, PowerShell 5.1+. Все операции идут через один вход —
`notioncode.ps1`.

## Требования

Git, Python 3.10+ (с «Add Python to PATH»), Node.js 18+ и npm, аккаунт Notion с
доступным Notion AI. Для Codex в VS Code — расширение `openai.chatgpt`.

## 1. Клонировать и установить

```powershell
git clone <GITHUB_REPOSITORY_URL>
Set-Location .\notioncode_mcp
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\notioncode.ps1 install
```

Параметры установки:

```powershell
.\notioncode.ps1 install -CodeRoot "C:\Projects"     # ограничить доступ к файлам
.\notioncode.ps1 install -NoStart                    # не запускать сразу
.\notioncode.ps1 install -NoAutoStart                # без записи в автозагрузку
.\notioncode.ps1 install -ForceModel ""              # разрешить выбор модели клиентом
```

## 2. Добавить Notion-сессию

```powershell
& ".\.runtime\notion-agent-cli-venv\Scripts\notion-agent.exe" `
  init --token-v2 - `
  --account "$HOME\.notionagents\notion_account.json"
```

Вставьте значение `token_v2`, нажмите Enter, затем `Ctrl+Z` и Enter. После этого:

```powershell
& ".\.runtime\notion-agent-cli-venv\Scripts\notion-agent.exe" `
  doctor --account "$HOME\.notionagents\notion_account.json" --json
.\notioncode.ps1 install
.\notioncode.ps1 verify
```

Успех: `verify` печатает JSON с `"ok": true`.

## 3. Команды

```powershell
.\notioncode.ps1 status                 # порты и /healthz как JSON
.\notioncode.ps1 start                  # запустить оба сервиса
.\notioncode.ps1 start -Foreground      # bridge в текущем окне (для отладки)
.\notioncode.ps1 stop
.\notioncode.ps1 restart
.\notioncode.ps1 verify                 # полная проверка установки
.\notioncode.ps1 verify -SkipLiveChecks # только файлы и конфиги
.\notioncode.ps1 logs                   # следить за логом bridge
.\notioncode.ps1 watch                  # держать сервисы живыми
```

## Непрерывная работа

У Windows нет systemd, поэтому роль супервизора выполняет `watch`: каждые 15
секунд он проверяет оба порта и `/livez`, и перезапускает сервисы, если что-то
перестало отвечать.

Установщик добавляет в автозагрузку пользователя
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\notioncode-mcp.cmd`,
который вызывает `notioncode.ps1 start`. Для реального 24/7 запустите `watch` как
задачу планировщика:

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$PWD\notioncode.ps1`" watch"
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "notioncode_mcp watch" -Action $action -Trigger $trigger `
  -Settings (New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1))
```

## Логи

```powershell
Get-Content .\.runtime\logs\bridge.err.log -Wait -Tail 50
Get-Content .\.runtime\logs\runtime.err.log -Wait -Tail 50
```

`start.ps1` ротирует файл при превышении 20 MB и держит 5 архивов, так что
многонедельная работа не заполнит диск.

## Codex в VS Code

Установите `openai.chatgpt`, завершите установку выше, выполните
`Developer: Reload Window` и откройте новый чат. После обновления расширения
запустите `.\notioncode.ps1 install` снова — он восстановит совместимость model
picker.

## Отличия от Linux

| | Linux | Windows |
|---|---|---|
| Супервизор | systemd (`Restart=always`, watchdog) | `notioncode.ps1 watch` |
| Логи | journald | файлы в `.runtime\logs` с ротацией |
| Автозапуск | `systemctl enable` | Startup-ярлык или задача планировщика |
| Профиль OpenCode | `.runtime/opencode` | `.runtime\opencode` (одинаково) |
| Shell для `run_shell` | `/bin/bash -lc` | `powershell.exe -Command` |

Общая реализация bridge и coding-runtime одна и та же; различается только запуск
процессов.

## Удаление

```powershell
.\notioncode.ps1 stop
Remove-Item "$([Environment]::GetFolderPath('Startup'))\notioncode-mcp.cmd" -ErrorAction SilentlyContinue
```

Затем удалите managed-блок из `$HOME\.codex\config.toml` (между маркерами
`BEGIN/END notioncode_mcp`). Каталог `$HOME\.notionagents` содержит ваши
Notion-сессии.
