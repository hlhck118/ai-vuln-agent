@echo off
setlocal
rem ====================================================================
rem  Tunnel launcher - a thin shim, and nothing else.
rem
rem  It hands the mode word "tunnel" to start.cmd. That side owns the
rem  health check, the loopback bind and the temporary public HTTPS entry.
rem  Nothing here repeats a listener address or a flag: two copies would
rem  disagree the moment one of them is edited, and this one is the copy
rem  nobody reads.
rem
rem  KEEP THIS FILE PURE ASCII, CRLF, no BOM. Same reason as start-lan.cmd:
rem  a redirected log is exactly where a non-ASCII batch line loses the
rem  lines around it.
rem ====================================================================
cd /d "%~dp0"
call "%~dp0start.cmd" tunnel %*
exit /b %errorlevel%
