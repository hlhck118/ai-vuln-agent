@echo off
setlocal
rem ====================================================================
rem  LAN launcher - a thin shim, and nothing else.
rem
rem  It hands the mode word "lan" to start.cmd, which owns the menu, the
rem  mode defaults and the risk text. Keep it that thin: a second copy of
rem  start.cmd's logic is exactly how the three launchers drifted apart the
rem  first time (double-clicking start.cmd worked, double-clicking this one
rem  did not).
rem
rem  KEEP THIS FILE PURE ASCII, CRLF, no BOM. With stdout redirected to a
rem  file, a batch line holding non-ASCII text makes cmd lose its own file
rem  offset, split the following lines mid-character and silently drop the
rem  neighbours. Scripts and CI always redirect; that is where it bites.
rem ====================================================================
cd /d "%~dp0"
call "%~dp0start.cmd" lan %*
exit /b %errorlevel%
