@echo off
rem ---------------------------------------------------------------------
rem  Project-local DSH launcher. For manual use only.
rem
rem  The platform does NOT go through this file: it runs
rem      node vendor\dsh\node_modules\@deepseek-ai\dsh\lib\bin.js
rem  directly (see pentestd/dsh_runtime.py: command_for).
rem
rem  This wrapper is here so a human can type one command:
rem      vendor\dsh\bin\dsh.cmd --version
rem      vendor\dsh\bin\dsh.cmd plugin --profile pentest-platform list
rem
rem  If node is missing it fails loudly instead of silently falling back to
rem  whatever `dsh` happens to be on PATH -- that fallback is exactly what
rem  this boundary is meant to remove.
rem ---------------------------------------------------------------------
setlocal
set "HERE=%~dp0"
set "BIN=%HERE%..\node_modules\@deepseek-ai\dsh\lib\bin.js"
if not exist "%BIN%" (
  echo [dsh] project-local DSH is not installed: "%BIN%" 1>&2
  echo [dsh] run vendor\dsh\install.cmd first 1>&2
  exit /b 3
)
where node >nul 2>nul
if errorlevel 1 (
  echo [dsh] node not found in PATH -- requires Node 22 or newer 1>&2
  exit /b 4
)
node "%BIN%" %*
exit /b %ERRORLEVEL%
