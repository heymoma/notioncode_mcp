<#
    Start both services in the background and wait for the bridge to answer.
    Windows has no systemd, so notioncode.ps1 watch supplies the restart loop.
#>
[CmdletBinding()]
param([switch]$Foreground)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$paths = Get-NotionCodePaths
if (-not (Test-Path $paths.McpEnv)) {
    throw "$($paths.McpEnv) is missing. Run scripts\install\windows.ps1 first."
}
if (-not (Test-Path $paths.PythonExe)) {
    throw "Python virtual environment is missing. Run scripts\install\windows.ps1 first."
}
if (-not (Test-Path $paths.ModelsPath)) {
    throw "$($paths.ModelsPath) is missing. Run scripts\install\windows.ps1 first."
}

New-Item -ItemType Directory -Force -Path $paths.LogDir, $paths.PidDir | Out-Null

$runtimeEnv = Get-DotEnv $paths.McpEnv
$bridgeEnv = Get-DotEnv $paths.BridgeEnv
$runtimePort = [int]($runtimeEnv["PORT"] ?? "8787")
$bridgePort = [int]($bridgeEnv["NOTION_BRIDGE_PORT"] ?? "8765")

if (-not (Test-TcpPort $runtimePort)) {
    $out = Join-Path $paths.LogDir "runtime.out.log"
    $err = Join-Path $paths.LogDir "runtime.err.log"
    Rotate-LogFile $out
    Rotate-LogFile $err
    foreach ($entry in $runtimeEnv.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "Process")
    }
    $runtime = Start-Process -FilePath "node.exe" -ArgumentList @($paths.BridgeServer) `
        -WorkingDirectory (Join-Path $paths.Root "services\mcp-runtime") -WindowStyle Hidden `
        -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    Set-Content -LiteralPath (Join-Path $paths.PidDir "runtime.pid") -Value $runtime.Id -Encoding ASCII
}

if (-not (Test-TcpPort $bridgePort)) {
    $out = Join-Path $paths.LogDir "bridge.out.log"
    $err = Join-Path $paths.LogDir "bridge.err.log"
    Rotate-LogFile $out
    Rotate-LogFile $err
    foreach ($entry in $bridgeEnv.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "Process")
    }
    $env:PYTHONPATH = Join-Path $paths.Root "src"
    $env:PYTHONUNBUFFERED = "1"
    $env:NOTIONCODE_ROOT = $paths.Root
    $arguments = @("-m", "notion_bridge")
    if ($Foreground) {
        & $paths.PythonExe @arguments
        exit $LASTEXITCODE
    }
    $bridge = Start-Process -FilePath $paths.PythonExe -ArgumentList $arguments `
        -WorkingDirectory $paths.Root -WindowStyle Hidden `
        -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    Set-Content -LiteralPath (Join-Path $paths.PidDir "bridge.pid") -Value $bridge.Id -Encoding ASCII
}

Wait-HttpOk "http://127.0.0.1:$bridgePort/healthz" 40 | ConvertTo-Json -Depth 10
