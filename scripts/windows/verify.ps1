<#
    Post-install verification. Exits non-zero with an explicit reason so it can
    be used as a gate in an automated setup.
#>
[CmdletBinding()]
param([switch]$SkipLiveChecks)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$paths = Get-NotionCodePaths
$expectedAliases = [ordered]@{
    "fable-5"     = "acai-budino-high"
    "gpt-5.6-sol" = "orange-mousse"
    "opus-5"      = "agave-flan"
}

$required = @(
    "src\notion_bridge\app.py",
    "src\notion_bridge\accounts\pool.py",
    "src\notion_bridge\notion\images.py",
    "services\mcp-runtime\src\server.js",
    "services\notion-private-mcp\run-from-account.js",
    "config\codex-models.json",
    ".runtime\env\bridge.env",
    ".runtime\env\mcp-runtime.env",
    ".runtime\notion-agent-cli-venv\Scripts\python.exe",
    ".runtime\opencode\opencode.jsonc"
)
$missing = @($required | Where-Object { -not (Test-Path (Join-Path $paths.Root $_)) })
if ($missing.Count -gt 0) {
    throw "Missing installed files: $($missing -join ', ')"
}
if (-not (Test-Path $paths.ModelsPath)) {
    throw "Model alias file is missing: $($paths.ModelsPath)"
}

$codexConfig = Join-Path $paths.CodexHome "config.toml"
if (-not (Test-Path $codexConfig)) {
    throw "Codex configuration is missing: $codexConfig"
}
$codexConfigText = Get-Content -LiteralPath $codexConfig -Raw -Encoding UTF8
if ($codexConfigText -notmatch 'model_provider\s*=\s*"notion-ai"' -or
    $codexConfigText -notmatch '\[model_providers\.notion-ai\]') {
    throw "Codex is not configured for the Notion provider: $codexConfig"
}
if ($codexConfigText -notmatch '(?s)\[mcp_servers\.notion-private\].*?enabled\s*=\s*true') {
    throw "The notion-private MCP server is disabled. Run notion-agent doctor, then re-run the installer."
}

$models = Get-Content -LiteralPath $paths.ModelsPath -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($entry in $expectedAliases.GetEnumerator()) {
    $actual = $models.friendly_aliases.PSObject.Properties[$entry.Key].Value
    if ($actual -ne $entry.Value) {
        throw "Incorrect model alias for $($entry.Key): expected $($entry.Value), got $actual"
    }
}

$catalog = Get-Content -LiteralPath (Join-Path $paths.Root "config\codex-models.json") `
    -Raw -Encoding UTF8 | ConvertFrom-Json
$slugs = @($catalog.models | ForEach-Object { $_.slug })
if (($slugs -join ",") -ne "gpt-5.5,gpt-5.6-sol,opus-5") {
    throw "Unexpected Codex model catalog: $($slugs -join ', ')"
}
foreach ($model in $catalog.models) {
    $efforts = @($model.supported_reasoning_levels | ForEach-Object { $_.effort })
    if (($efforts -join ",") -ne "low,medium,high") {
        throw "Unexpected reasoning efforts for $($model.slug): $($efforts -join ', ')"
    }
}

$bridgeEnv = Get-DotEnv $paths.BridgeEnv
$runtimeEnv = Get-DotEnv $paths.McpEnv
$bridgePort = [int]($bridgeEnv["NOTION_BRIDGE_PORT"] ?? "8765")
$runtimePort = [int]($runtimeEnv["PORT"] ?? "8787")

if (-not $SkipLiveChecks) {
    if (-not (Test-TcpPort $runtimePort)) { throw "Coding runtime is not listening on 127.0.0.1:$runtimePort" }
    if (-not (Test-TcpPort $bridgePort)) { throw "Notion bridge is not listening on 127.0.0.1:$bridgePort" }
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$bridgePort/healthz" -TimeoutSec 10
    if (-not $health.ok) { throw "Bridge health check reports no valid Notion accounts." }
    if (-not $health.ready) { throw "No Notion account is currently available; check /readyz for the reason." }
    $remoteModels = Invoke-RestMethod -Uri "http://127.0.0.1:$bridgePort/v1/models" -TimeoutSec 10
    $remoteIds = @($remoteModels.data | ForEach-Object { $_.id })
    if (($remoteIds -join ",") -ne "fable-5,gpt-5.6-sol,opus-5") {
        throw "Bridge returned unexpected models: $($remoteIds -join ', ')"
    }
    Invoke-RestMethod -Uri "http://127.0.0.1:$runtimePort/healthz" -TimeoutSec 10 | Out-Null
}

[pscustomobject]@{
    ok            = $true
    project_root  = $paths.Root
    model_aliases = $expectedAliases
    models        = $slugs
    bridge_port   = $bridgePort
    runtime_port  = $runtimePort
    live_checks   = -not $SkipLiveChecks
} | ConvertTo-Json -Depth 10
