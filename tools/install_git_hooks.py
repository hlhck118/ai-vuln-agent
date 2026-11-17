# -*- coding: utf-8 -*-
"""把「提交之后自动推一次」装进 `.git/hooks/post-commit`。

**为什么要有这个脚本，而不是手工 `cp` 一个文件。** 钩子只存在于 `.git/hooks/`，
那是一棵**不进版本库**的目录：clone 下来没有它，换台机器没有它，别人拿到交付包
也没有它。所以钩子的内容必须和代码一起留在版本库里（就是本文件），由这个脚本落
到 `.git/hooks/` 去。

## 两条硬性质，缺一条这个钩子就该拆掉

1. **绝不挡提交。** 提交是本地事实，推送是网络事实，两者没有依赖关系。凭据过期、
   断网、远端拒绝都不该让一次已经写好的提交回滚——那样会丢工作。所以：不 `set -e`，
   把 push 的退出码吃掉，无条件 `exit 0`。

2. **绝不让人等。** 这台机器的 GitHub 通道是间歇性的（分流软件把 github.com 映射到
   198.18.0.82，TLS 握手会中途被掐），一次 push 慢到分钟级是常态。**所以在后台推**：
   钩子立刻返回，推送在子进程里跑完，结果同步落进 `.git/push.log`。

试过但不对的写法，留在钩子注释里免得再踩：

- `VAR=x out=$(cmd)` —— 这只是两个赋值，`VAR` 到不了 `cmd` 里。必须在括号内：
  `out=$(VAR=x cmd)`。写在外面时 push 仍挂在「Username for …」那句提示上，
  一次提交被拖满 60 秒才被 `timeout` 杀掉。
- 前台 `timeout 60 git push` —— 能保证不永久挂住，但仍然让人等满 60 秒。

## 用法

    python tools/install_git_hooks.py            # 安装（已存在则覆盖，先备份）
    python tools/install_git_hooks.py --check    # 只看当前装的是什么，不改动
    python tools/install_git_hooks.py --remove   # 拆掉

退出码：0 = 装好/已是最新；1 = 不是 git 仓库或钩子缺失；2 = `--check` 发现不一致。
"""
import argparse
import os
import shutil
import stat
import subprocess
import sys
import time

HOOK_NAME = "post-commit"

HOOK = """#!/bin/sh
# 提交之后自动推一次。（由 tools/install_git_hooks.py 安装，改内容请改那个脚本）
#
# 为什么放在钩子里而不是每次自己记得敲：推送这件事唯一的判据就是「提交了没有」，
# 而那恰好是这个钩子被调用的时刻。
#
# **两条硬性质，缺一条这个钩子就该拆掉：**
#
#  1. 绝不挡提交。提交是本地事实，推送是网络事实，两者没有依赖关系。凭据过期、
#     断网、远端拒绝都不该让一次已经写好的提交回滚——那样会丢工作。所以不 set -e，
#     把 push 的退出码吃掉，最后无条件 exit 0。
#
#  2. 绝不让人等。GitHub 通道是间歇性的，一次 push 慢到分钟级是常态。**所以在后台
#     推**：钩子自己立刻返回，推送在子进程里跑完，结果同步落进 .git/push.log。
#
# 试过但不对的写法，留着免得再踩：
#   `VAR=x out=$(cmd)` —— 这只是两个赋值，VAR 到不了 cmd 里。必须在括号内：
#     `out=$(VAR=x cmd)`。上一版写在外面，push 仍挂在「Username for …」那句提示上。
#   前台 `timeout 60 git push` —— 能保证不永久挂住，但仍然让人等满 60 秒。

set +e

# 不递归：push 不会触发新提交，但保险起见把这次推送从 hook 触发链里摘出去。
unset GIT_DIR GIT_WORK_TREE

repo=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
log="$repo/.git/push.log"

(
  # GIT_TERMINAL_PROMPT=0：没有凭据时立刻失败，而不是弹出「Username for
  # https://github.com」等一个永远不会有人来敲的回车。
  # timeout 300：给慢网络留足时间，但终究要有个尽头。
  {
    echo "[$(date "+%Y-%m-%d %H:%M:%S")] push $(git rev-parse --short HEAD)"
    GIT_TERMINAL_PROMPT=0 timeout 300 \\
      git -c credential.helper=wincred push --quiet origin HEAD 2>&1
    echo "[$(date "+%Y-%m-%d %H:%M:%S")] rc=$?"
  } >>"$log" 2>&1
  tail -n 200 "$log" >"$log.tmp" 2>/dev/null && mv "$log.tmp" "$log"
) </dev/null >/dev/null 2>&1 &

exit 0
"""


def hooks_dir():
    """当前仓库的 hooks 目录（跟着 core.hooksPath 走）。"""
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             check=True).stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.CalledProcessError):
        return ""
    if not top:
        return ""
    custom = subprocess.run(["git", "config", "--get", "core.hooksPath"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    path = custom.stdout.decode("utf-8", "replace").strip()
    if path:
        return path if os.path.isabs(path) else os.path.join(top, path)
    return os.path.join(top, ".git", "hooks")


def current(hook_path):
    try:
        with open(hook_path, "r", encoding="utf-8", newline="") as fh:
            return fh.read()
    except OSError:
        return ""


def arm(hook_path):
    """补上可执行位。Windows 上看不出效果，但 Linux/macOS 上没有它钩子根本不会跑。"""
    mode = os.stat(hook_path).st_mode
    os.chmod(hook_path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def main(argv=None):
    ap = argparse.ArgumentParser(description="安装/检查/拆除 post-commit 自动推送钩子")
    ap.add_argument("--check", action="store_true", help="只比对，不写")
    ap.add_argument("--remove", action="store_true", help="拆掉钩子")
    args = ap.parse_args(argv)

    folder = hooks_dir()
    if not folder:
        print("这里不是 git 仓库（git rev-parse --show-toplevel 没给出路径）。")
        return 1
    target = os.path.join(folder, HOOK_NAME)
    existing = current(target)

    if args.check:
        if existing == HOOK:
            print("已装，且与 tools/install_git_hooks.py 里的内容逐字一致：%s" % target)
            return 0
        if not existing:
            print("没装：%s 不存在。跑一次 `python tools/install_git_hooks.py`。" % target)
            return 2
        print("内容与版本库里的版本不一致：%s" % target)
        print("（可能是旧版本，也可能被手工改过。跑一次安装会覆盖，旧的那份先备份。）")
        return 2

    if args.remove:
        if not os.path.isfile(target):
            print("本来就没装：%s" % target)
            return 0
        os.remove(target)
        print("已拆除：%s" % target)
        return 0

    if existing == HOOK:
        arm(target)
        print("已是最新，未改动：%s" % target)
        return 0

    os.makedirs(folder, exist_ok=True)
    backup = ""
    if existing:
        backup = "%s.bak-%s" % (target, time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(target, backup)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(HOOK)
    arm(target)

    print("已安装：%s" % target)
    if backup:
        print("旧的那份备份在：%s" % backup)
    print("作用：commit 之后在后台推一次 origin HEAD，结果追加到 .git/push.log。")
    print("钩子立刻返回、永不挡提交；`python tools/install_git_hooks.py --remove` 可拆。")
    return 0


if __name__ == "__main__":
    sys.exit(main())