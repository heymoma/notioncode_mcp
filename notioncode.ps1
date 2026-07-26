<#
.SYNOPSIS
    Single entry point for notioncode_mcp on Windows.

.DESCRIPTION
    Windows has no service supervisor comparable to systemd, so `watch` provides
    the always-on behaviour: it restarts either half of the service whenever its
    port stops answering.

.EXAMPLE
    .\notioncode.ps1 install -CodeRoot C:\Projects
    .\notioncode.ps1 status
    .\notioncode.ps1 watch
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "start", "stop", "restart", "status", "verify", "watch", "logs", "help")]
    [string]$Command = "help",

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "scripts\windows\common.ps1")

$paths = Get-NotionCodePaths
$windows = Join-Path $PSScriptRoot "scripts\windows"

function Invoke-Script([string]$Name, [string[]]$Arguments) {
    & (Join-Path $windows $Name) @Arguments
}

switch ($Command) {
    "install" { & (Join-Path $PSScriptRoot "scripts\install\windows.ps1") @Rest }
    "start"   { Invoke-Script "start.ps1" $Rest }
    "stop"    { Invoke-Script "stop.ps1" $Rest }
    "restart" {
        Invoke-Script "stop.ps1" @()
        Invoke-Script "start.ps1" $Rest
    }
    "status"  { Invoke-Script "status.ps1" $Rest }
    "verify"  { Invoke-Script "verify.ps1" $Rest }
    "logs" {
        $log = Join-Path $paths.LogDir "bridge.err.log"
        if (-not (Test-Path $log)) { throw "No log file yet: $log" }
        Get-Content -LiteralPath $log -Wait -Tail 50
    }
    "watch" {
        $bridgeEnv = Get-DotEnv $paths.BridgeEnv
        $runtimeEnv = Get-DotEnv $paths.McpEnv
        $bridgePort = [int]($bridgeEnv["NOTION_BRIDGE_PORT"] ?? "8765")
        $runtimePort = [int]($runtimeEnv["PORT"] ?? "8787")
        $interval = 15
        Write-Host "Watching 127.0.0.1:$bridgePort and 127.0.0.1:$runtimePort every ${interval}s. Ctrl+C to stop."
        while ($true) {
            $bridgeUp = Test-TcpPort $bridgePort
            $runtimeUp = Test-TcpPort $runtimePort
            $live = $false
            if ($bridgeUp) {
                try {
                    $live = (Invoke-RestMethod -Uri "http://127.0.0.1:$bridgePort/livez" -TimeoutSec 5).ok
                }
                catch { $live = $false }
            }
            if (-not $runtimeUp -or -not $bridgeUp -or -not $live) {
                $stamp = (Get-Date).ToString("s")
                Write-Warning "$stamp bridge_up=$bridgeUp runtime_up=$runtimeUp responsive=$live; restarting"
                try {
                    Invoke-Script "stop.ps1" @()
                    Invoke-Script "start.ps1" @() | Out-Null
                }
                catch {
                    Write-Warning "restart failed: $($_.Exception.Message)"
                }
            }
            Start-Sleep -Seconds $interval
        }
    }
    default {
        Write-Host "Usage: .\notioncode.ps1 <command>"
        Write-Host ""
        Write-Host "  install [-CodeRoot <path>] [-NoStart] [-NoAutoStart]  install or update"
        Write-Host "  start [-Foreground]                                   start both services"
        Write-Host "  stop                                                  stop both services"
        Write-Host "  restart                                               stop, then start"
        Write-Host "  status                                                ports and /healthz as JSON"
        Write-Host "  verify [-SkipLiveChecks]                              full post-install check"
        Write-Host "  watch                                                 keep both services alive"
        Write-Host "  logs                                                  follow the bridge log"
    }
}
