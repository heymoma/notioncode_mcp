$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$paths = Get-NotionCodePaths
foreach ($name in @("bridge", "runtime")) {
    $pidFile = Join-Path $paths.PidDir "$name.pid"
    if (-not (Test-Path $pidFile)) { continue }
    $processId = [int](Get-Content -LiteralPath $pidFile -Raw)
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($process) {
        # CloseMainWindow is not available for a hidden console process, so the
        # bridge relies on its own shutdown drain during the grace period.
        Stop-Process -Id $processId -Force
        Write-Host "Stopped $name (PID $processId)."
    }
    Remove-Item -LiteralPath $pidFile -Force
}
