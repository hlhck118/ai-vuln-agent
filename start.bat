@echo off
rem ---
rem 一键启动 Web 控制台（Windows）。
rem
rem 和仓库根的 start.sh 是同一条路：这个脚本 -> deploy\start.py。它只做四件事：
rem 切到脚本所在目录、挑一个 Python 3.10+、把参数原样交出去、以及在你要求远程访问
rem 时顺手开一条公网隧道。「本机 / 局域网 / 外网隧道」那套菜单仍然是撤掉的：
rem 监听哪个地址是 --host 的事，映射到公网是 tools\keep_tunnel.py 的事，这一层只
rem 提供 --lan / --remote 两个把两件事拼起来的短写法，别的参数一律原样转发。
rem
rem   start.bat                    起控制台并打开浏览器（只听 127.0.0.1）
rem   start.bat --port 9000
rem   start.bat --no-browser       只起服务（适合做成计划任务）
rem   start.bat --lan              同一内网的手机 / 平板 / 别的电脑也能打开
rem   start.bat --remote           内网 + 公网：绑 0.0.0.0，并另开一个窗口起隧道
rem   start.bat --tunnel           只起隧道（控制台已经在别处跑着）
rem
rem 退出码原样交出去：0 正常退出；1 配置或环境有问题；2 = 这个端口上已经有一个
rem 本平台实例在跑（那不是失败，是「服务已经在了」，见 deploy\start.py 的说明）。
setlocal
chcp 65001 >nul 2>nul
pushd "%~dp0"

rem Python 的输出编码不能由终端决定：横幅里有 ⚠ 这类字符，strict 会在
rem console.make_safe() 跑到之前就把进程打死（加了 --host 0.0.0.0 就起不来）。
rem replace 让它退化成问号，而不是异常。
set "PYTHONIOENCODING=utf-8:replace"
set "PYTHONUTF8=1"

rem 参数机：认得的自己消化，认不得的原样攒进 ARGS 交给 deploy\start.py。
rem 逐个 token 单独加引号，路径里有空格也不会被再切一次。
set "LAN="
set "REMOTE="
set "TUNNEL="
set "SERVER="
set "HAVE_HOST="
set "TUNNEL_PORT=8787"
set "ARGS="

:gather
if "%~1"=="" goto gathered
if /i "%~1"=="--lan" goto flag_lan
if /i "%~1"=="--remote" goto flag_remote
if /i "%~1"=="--tunnel" goto flag_tunnel
if /i "%~1"=="--host" goto take_host
if /i "%~1"=="--port" goto take_port
set "ARGS=%ARGS% "%~1%""
shift
goto gather

:flag_lan
set "LAN=1"
shift
goto gather

:flag_remote
set "LAN=1"
set "REMOTE=1"
set "TUNNEL=1"
set "SERVER=1"
shift
goto gather

:flag_tunnel
set "TUNNEL=1"
set "SERVER=0"
shift
goto gather

:take_host
if "%~2"=="" goto missing_value
set "HAVE_HOST=1"
set "ARGS=%ARGS% "--host" "%~2""
shift
shift
goto gather

:take_port
if "%~2"=="" goto missing_value
set "TUNNEL_PORT=%~2"
set "ARGS=%ARGS% "--port" "%~2""
shift
shift
goto gather

:gathered
rem 用户自己给了 --host 就不再补，免得两个 --host 抢同一个参数位。
if defined LAN if not defined HAVE_HOST set "ARGS=%ARGS% "--host" "0.0.0.0""

call :find_python
if not defined PYEXE goto no_python

if not defined TUNNEL goto run_server
call :launch_tunnel
if defined SERVER goto run_server
rem 只起隧道：控制台已经在别处跑着了，这里不重复起一份（那会撞退出码 2）。
set "RC=0"
goto finish

:run_server
"%PYEXE%" %PYARGS% "deploy\start.py" %ARGS%
set "RC=%ERRORLEVEL%"
goto finish

:find_python
rem deploy\start.py 用到 3.10 才有的语法，所以先验版本再采用；
rem python 在 Windows 上常常是 MS Store 的转发器（跑起来又弹商店），
rem 版本验不过自然就被跳过了。
set "PYEXE="
set "PYARGS="
call :try_python python ""
if defined PYEXE goto :eof
call :try_python python3 ""
if defined PYEXE goto :eof
call :try_python py "-3"
goto :eof

:try_python
where %~1 >nul 2>nul
if errorlevel 1 goto :eof
%~1 %~2 -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 goto :eof
set "PYEXE=%~1"
set "PYARGS=%~2"
goto :eof

:launch_tunnel
rem 隧道对端连的始终是 127.0.0.1:<端口>，所以它和「局域网」是两条路。
rem 隧道必须在控制台起来之前就派出去：它能自己重连，等不到本地端口也只是先失败几次。
set "URLFILE=data\tunnel-url.txt"
echo 远程访问：正在另一个窗口里开 SSH 反向隧道。
start "ai-vuln-agent 远程访问隧道" "%PYEXE%" %PYARGS% "tools\keep_tunnel.py" --port %TUNNEL_PORT% --url-file "%URLFILE%"
echo   公网地址大约十几秒后写进 %URLFILE%，用下面这条查：
echo     type %URLFILE%
echo   那个窗口关掉，外网入口就没了（控制台本身还在本机跑着）。
goto :eof

:missing_value
echo 参数 %~1 后面要跟一个值（例如 %~1 9000）。 1>&2
set "RC=1"
goto finish

:no_python
echo Python 3.10+ was not found on PATH. 1>&2
echo   install it from https://www.python.org/downloads/ 1>&2
set "RC=1"
goto finish

:finish
popd
endlocal & exit /b %RC%
