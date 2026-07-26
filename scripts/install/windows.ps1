<#
    Install notioncode_mcp on Windows.

    Idempotent: safe to re-run after a git pull, after adding a Notion session
    or after updating the Codex extension.
#>
[CmdletBinding()]
param(
    [string]$CodeRoot = $HOME,
    [int]$BridgePort = 8765,
    [int]$RuntimePort = 8787,
    [string]$ForceModel = "opus-5",
    [switch]$NoAutoStart,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "..\windows\common.ps1")

$paths = Get-NotionCodePaths
Write-Host "Installing notioncode_mcp for Windows from $($paths.Root)"

Assert-Command "node.exe" "Install Node.js 18 or newer."
Assert-Command "npm.cmd" "Install Node.js 18 or newer."
$python = Find-Python
Invoke-Python $python @("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)")

Write-Host "==> Preparing directories"
New-Item -ItemType Directory -Force -Path @(
    $paths.Runtime, $paths.EnvDir, $paths.LogDir, $paths.PidDir,
    $paths.OpenCodeHome, $paths.AccountHome,
    (Join-Path $paths.AccountHome "accounts"), $paths.CodexHome
) | Out-Null

Write-Host "==> Installing Python dependencies"
if (-not (Test-Path $paths.PythonExe)) {
    Invoke-Python $python @("-m", "venv", $paths.Venv)
}
& $paths.PythonExe -m pip install --disable-pip-version-check -q -r (Join-Path $paths.Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed." }
& $paths.PythonExe -m pip install --disable-pip-version-check -q --no-deps -e $paths.Root
if ($LASTEXITCODE -ne 0) { throw "Bridge package installation failed." }

Write-Host "==> Installing Node dependencies"
& npm.cmd --prefix (Join-Path $paths.Root "services\mcp-runtime") ci --omit=dev --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { throw "Coding runtime npm dependency installation failed." }
& npm.cmd --prefix (Join-Path $paths.Root "services\notion-private-mcp") ci --omit=dev --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { throw "Private Notion MCP npm dependency installation failed." }
# OpenCode loads its provider plugin from the profile directory, so these have
# to be installed on Windows exactly as they are on Linux.
& npm.cmd --prefix $paths.OpenCodeHome install --no-audit --no-fund `
    "@ai-sdk/openai-compatible" "@opencode-ai/plugin"
if ($LASTEXITCODE -ne 0) { throw "OpenCode provider dependency installation failed." }

Write-Host "==> Writing service environment"
$secret = ""
if (Test-Path $paths.McpEnv) {
    $secret = (Get-DotEnv $paths.McpEnv)["MCP_PATH_SECRET"]
}
elseif (Test-Path $paths.LegacyEnv) {
    $legacy = Get-DotEnv $paths.LegacyEnv
    $secret = $legacy["MCP_PATH_SECRET"]
    if ($legacy["CODE_ROOT"] -and -not $PSBoundParameters.ContainsKey("CodeRoot")) {
        $CodeRoot = $legacy["CODE_ROOT"]
    }
    Write-Host "    migrated the MCP secret from services\mcp-runtime\.env"
}
if (-not $secret) { $secret = New-RandomHex 32 }
$resolvedCodeRoot = [IO.Path]::GetFullPath($CodeRoot)

Write-EnvFile $paths.McpEnv @(
    "MCP_PATH_SECRET=$secret"
    "CODE_ROOT=$resolvedCodeRoot"
    "HOST=127.0.0.1"
    "PORT=$RuntimePort"
)
Write-EnvFile $paths.BridgeEnv @(
    "NOTION_AGENT_HOME=$($paths.AccountHome)"
    "NOTION_BRIDGE_HOST=127.0.0.1"
    "NOTION_BRIDGE_PORT=$BridgePort"
    "NOTION_MCP_RUNTIME_URL=http://127.0.0.1:$RuntimePort/mcp/$secret"
    "CODE_ROOT=$resolvedCodeRoot"
    "NOTION_FORCE_MODEL=$ForceModel"
    "NOTION_LOG_LEVEL=INFO"
)

Write-Host "==> Installing model aliases and migrating accounts"
& node.exe (Join-Path $paths.Root "scripts\codex\install-model-aliases.mjs") `
    (Join-Path $paths.Root "state-template\.notionagents\models.json") $paths.ModelsPath
if ($LASTEXITCODE -ne 0) { throw "Model alias installation failed." }
$env:PYTHONPATH = Join-Path $paths.Root "src"
& $paths.PythonExe -m notion_bridge.accounts.migrate $paths.AccountHome
if ($LASTEXITCODE -ne 0) { throw "Notion account migration failed." }

$hasAccount = Test-Path (Join-Path $paths.AccountHome "notion_account.json")
if (-not $hasAccount) {
    $hasAccount = @(Get-ChildItem -LiteralPath (Join-Path $paths.AccountHome "accounts") `
        -Filter "*.json" -File -ErrorAction SilentlyContinue).Count -gt 0
}
$notionMcpEnabled = if ($hasAccount) { "true" } else { "false" }

Write-Host "==> Rendering client configuration"
& node.exe (Join-Path $paths.Root "scripts\codex\install-config.mjs") `
    (Join-Path $paths.Root "config\codex-cli-config.toml") `
    (Join-Path $paths.CodexHome "config.toml") $paths.Root $HOME $notionMcpEnabled
if ($LASTEXITCODE -ne 0) { throw "Codex configuration generation failed." }
& node.exe (Join-Path $paths.Root "scripts\codex\patch-webview.mjs") $HOME
if ($LASTEXITCODE -ne 0) { throw "Codex VS Code webview compatibility patch failed." }
& node.exe (Join-Path $paths.Root "scripts\render-config.mjs") `
    (Join-Path $paths.Root "config\opencode.jsonc") `
    (Join-Path $paths.OpenCodeHome "opencode.jsonc") $paths.Root $HOME
if ($LASTEXITCODE -ne 0) { throw "OpenCode configuration generation failed." }

$startupCmd = Join-Path ([Environment]::GetFolderPath("Startup")) "notioncode-mcp.cmd"
if (-not $NoAutoStart) {
    $escapedStart = (Join-Path $paths.Root "notioncode.ps1").Replace('"', '""')
    @(
        "@echo off"
        "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$escapedStart`" start"
    ) | Set-Content -LiteralPath $startupCmd -Encoding ASCII
    Write-Host "    autostart entry: $startupCmd"
}

if (-not $NoStart) {
    & (Join-Path $PSScriptRoot "..\windows\stop.ps1")
    & (Join-Path $PSScriptRoot "..\windows\start.ps1") | Out-Null
}

Write-Host ""
Write-Host "Installation complete."
Write-Host "  project root      $($paths.Root)"
Write-Host "  code root         $resolvedCodeRoot"
Write-Host "  accounts          $($paths.AccountHome)"
Write-Host "  bridge            http://127.0.0.1:$BridgePort  (/healthz /readyz /metrics)"
Write-Host "  coding runtime    http://127.0.0.1:$RuntimePort (/healthz)"
Write-Host "  codex config      $(Join-Path $paths.CodexHome 'config.toml')"
Write-Host "  opencode profile  $($paths.OpenCodeHome)"
Write-Host "  manage            .\notioncode.ps1 status | start | stop | verify"

if (-not $hasAccount) {
    Write-Warning "No Notion session is configured yet, so the bridge answers 503."
    Write-Host "Run the command below, paste token_v2, then press Ctrl+Z and Enter:"
    Write-Host "& '$($paths.Venv)\Scripts\notion-agent.exe' init --token-v2 - --account '$($paths.AccountHome)\notion_account.json'"
    Write-Host "Then run notion-agent doctor and re-run this installer to enable MCP."
}
