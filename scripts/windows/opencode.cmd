@echo off
rem Launch OpenCode against the notioncode_mcp provider profile, without
rem touching the user's global OpenCode configuration.
setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "NOTIONCODE_ROOT=%%~fI"
set "OPENCODE_CONFIG_DIR=%NOTIONCODE_ROOT%\.runtime\opencode"
if not exist "%OPENCODE_CONFIG_DIR%\opencode.jsonc" (
  echo OpenCode profile is missing. Run .\notioncode.ps1 install first. 1>&2
  exit /b 1
)
where opencode >nul 2>nul
if errorlevel 1 (
  echo OpenCode was not found in PATH. Install OpenCode and retry. 1>&2
  exit /b 127
)
opencode %*
exit /b %errorlevel%
