#!/bin/sh
# 一键启动 Web 控制台。
#
# 仓库根只有这一条启动路径：这个脚本 -> deploy/start.py。
# 「本机 / 局域网 / 外网隧道」那套启动方式菜单已经撤掉：监听哪个地址是 --host
# 的事，要不要映射到公网是 tools/keep_tunnel.py 的事，两样都不该由这一层再问一遍。
# 不带参数时就是最保守的那一档——只听 127.0.0.1。
#
#   ./start.sh                    # 起控制台并打开浏览器
#   ./start.sh --port 9000
#   ./start.sh --no-browser       # 只起服务（适合做成系统服务）
#   ./start.sh --host 0.0.0.0     # 同一内网的手机/别的电脑也能打开（务必配认证）
#
# 退出码原样交出去：0 正常退出；1 配置或环境有问题；2 = 这个端口上已经有一个
# 本平台实例在跑（那不是失败，是「服务已经在了」，见 deploy/start.py 的说明）。
set -u

# 先站到脚本所在目录。双击、桌面快捷方式、从别的目录调用时 cwd 是随机的，
# 而 --db 与 Web 资源的默认路径都按仓库根算。
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
cd "$here" || exit 1

# Python 的输出编码不能由终端决定：横幅里有 ⚠ 这类字符，strict 会在
# console.make_safe() 跑到之前就把进程打死（加了 --host 0.0.0.0 就起不来）。
# replace 让它退化成问号，而不是异常。
PYTHONIOENCODING=utf-8:replace
PYTHONUTF8=1
export PYTHONIOENCODING PYTHONUTF8

# 找解释器。deploy/start.py 用到 3.10 才有的语法，所以要验版本；
# 一个个试，第一个合格的直接 exec 进去（pid 不换，Ctrl+C 打到的就是控制面）。
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
            exec "$candidate" "$here/deploy/start.py" "$@"
        fi
    fi
done

echo "Python 3.10+ was not found on PATH." >&2
echo "  install it from https://www.python.org/downloads/" >&2
exit 1
