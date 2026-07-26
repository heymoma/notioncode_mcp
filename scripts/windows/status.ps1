$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$paths = Get-NotionCodePaths
$bridgeEnv = Get-DotEnv $paths.BridgeEnv
$runtimeEnv = Get-DotEnv $paths.McpEnv
$bridgePort = [int]($bridgeEnv["NOTION_BRIDGE_PORT"] ?? "8765")
$runtimePort = [int]($runtimeEnv["PORT"] ?? "8787")

$result = [ordered]@{
    project_root  = $paths.Root
    bridge_port   = $bridgePort
    bridge_up     = Test-TcpPort $bridgePort
    runtime_port  = $runtimePort
    runtime_up    = Test-TcpPort $runtimePort
    account_file  = Test-Path (Join-Path $paths.AccountHome "notion_account.json")
    models_file   = Test-Path $paths.ModelsPath
    bridge_env    = Test-Path $paths.BridgeEnv
}
if ($result.bridge_up) {
    try { $result.health = Invoke-RestMethod -Uri "http://127.0.0.1:$bridgePort/healthz" -TimeoutSec 5 }
    catch { $result.health_error = $_.Exception.Message }
}
if ($result.runtime_up) {
    try { $result.runtime_health = Invoke-RestMethod -Uri "http://127.0.0.1:$runtimePort/healthz" -TimeoutSec 5 }
    catch { $result.runtime_health_error = $_.Exception.Message }
}
[pscustomobject]$result | ConvertTo-Json -Depth 10
