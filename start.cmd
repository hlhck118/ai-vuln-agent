@echo off
setlocal
rem ====================================================================
rem  AI pentest platform - one launcher, three ways to start (menu: 1 / 2 / 3):
rem
rem    start.cmd              menu: 1 local / 2 lan / 3 tunnel  (Enter = 2)
rem    start.cmd local        loopback only (127.0.0.1)
rem    start.cmd lan          whole LAN (0.0.0.0); back to loopback by
rem                           appending --host 127.0.0.1
rem    start.cmd tunnel       single window: if no console is running it starts
rem                           one on 127.0.0.1, then exposes it over public HTTPS
rem
rem  KEEP THIS FILE PURE ASCII. Every Chinese line (menu, mode banner, the
rem  LAN risk text, tunnel notes) lives in deploy\start.py, which prints it.
rem  This is not style: with stdout redirected to a file, a batch line that
rem  holds non-ASCII text makes cmd lose its own file offset, split the next
rem  lines mid-character, print a "is not recognized as an internal or
rem  external command" error for a fragment of text, and silently drop the
rem  neighbouring lines. Same file in a console: fine. So the bug only
rem  appears in logs / CI / any shell redirect to a file, which is where
rem  you look last.
rem
rem  start-lan.cmd and start-tunnel.cmd are thin shims that call this
rem  file with the mode word, so the old double-click paths keep working.
rem  Keep them that thin: a second copy of this file's logic is exactly how
rem  the three launchers drifted apart the first time.
rem ====================================================================
cd /d "%~dp0"

rem  Find an interpreter without pinning a path. The py launcher finds the
rem  newest 3.x; python / python3 cover installs without it. `-c "import sys"`
rem  is the probe: a broken or Microsoft-Store stub fails it instead of
rem  launching a console window that closes instantly.
set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY ( python -c "import sys" >nul 2>&1 && set "PY=python" )
if not defined PY ( python3 -c "import sys" >nul 2>&1 && set "PY=python3" )
if not defined PY goto :no_python

rem  Both sides of the pipe agree on one encoding: the console gets UTF-8
rem  output from Python, and a redirected log gets the same bytes. `replace`
rem  matters - strict would turn one unsupported glyph into a crash before
rem  pentestd.console.make_safe() ever runs.
set "PYTHONIOENCODING=utf-8:replace"
set "PYTHONUTF8=1"

rem  --launcher hands the mode word and every argument to deploy\start.py,
rem  which owns the menu, the mode defaults and the tunnel. %* stays last so
rem  `start.cmd --port 9000` keeps meaning what it always did.
%PY% "deploy\start.py" --launcher %*
set "CODE=%ERRORLEVEL%"

:done
call :pause_if_interactive
exit /b %CODE%

:no_python
echo ====================================================================
echo  Python 3.10+ was not found on PATH.
echo.
echo  Install it from https://www.python.org/downloads/windows/
echo  and tick "Add python.exe to PATH", then run this file again.
echo  More detail: the project overview doc - "Windows: one-click start".
echo ====================================================================
set "CODE=1"
goto :done

:pause_if_interactive
rem  A double-clicked window disappears the moment the process exits, and the
rem  last thing printed is usually the reason it exited. Tests and CI set
rem  PENTEST_NO_PAUSE=1 so they never wait for a keypress.
if defined PENTEST_NO_PAUSE exit /b 0
pause
exit /b 0
