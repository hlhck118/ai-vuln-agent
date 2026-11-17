@echo off
rem ---------------------------------------------------------------------
rem  Install / update the project-local DSH. This is the ONLY entry point
rem  that should be used to upgrade DSH for this project.
rem
rem  Why: the platform's runtime, SDK patch, plugin CLI and pnpm all come
rem  from vendor\dsh\node_modules (see pentestd/dsh_runtime.py: locate_dsh).
rem  Which DSH this project runs is therefore decided by the version pinned
rem  in vendor\dsh\package.json -- reproducible, rollbackable, and with no
rem  relationship to the desktop DeepSeek Harness installed on this machine.
rem
rem  Usage:
rem      install.cmd              install/update to the pinned versions
rem      install.cmd 0.2.1-rc.2   switch versions (rewrites package.json)
rem ---------------------------------------------------------------------
pushd "%~dp0"

where node >nul 2>nul
if errorlevel 1 (
  echo [dsh] node not found in PATH. Install Node.js 22+: https://nodejs.org/ 1>&2
  popd & exit /b 4
)
where npm >nul 2>nul
if errorlevel 1 (
  echo [dsh] npm not found in PATH -- it ships with node. 1>&2
  popd & exit /b 4
)
node -e "const m=+process.versions.node.split('.')[0];if(m<22){console.error('[dsh] Node '+process.versions.node+' is too old; this project needs >=22');process.exit(5)}"
if errorlevel 1 ( popd & exit /b 5 )

if not "%~1"=="" (
  echo [dsh] switching @deepseek-ai/dsh to version %~1
  call npm install --no-audit --no-fund --save-exact "@deepseek-ai/dsh@%~1" "@deepseek-ai/dsh-sdk-app@%~1" "@deepseek-ai/dsh-sdk-jsonrpc-server@%~1"
) else (
  call npm install --no-audit --no-fund
)
if errorlevel 1 (
  echo [dsh] npm install failed -- the project-local DSH was NOT updated. 1>&2
  popd & exit /b 1
)

set "BIN=%~dp0node_modules\@deepseek-ai\dsh\lib\bin.js"
if not exist "%BIN%" (
  echo [dsh] install finished but entry point is missing: %BIN% 1>&2
  popd & exit /b 6
)
node -e "const p=require('./node_modules/@deepseek-ai/dsh/package.json');console.log('[dsh] project-local DSH version: '+p.version)"
if errorlevel 1 ( popd & exit /b 1 )
echo [dsh] done. The platform picks this up on next start / channel rebuild.
popd & exit /b 0
