# -*- coding: utf-8 -*-
"""把平台的 DSH 界面插件（`dsh-plugins/dsh-pentest-console`）装进某一份 DSH 的
web profile。

**为什么需要这个脚本，而不是手改 profile。** DSH 的 profile 是一层组合
（`package.json` 里的依赖 + `dsh.profile.bundles` + `cordis.patch.yml`），三处
要对得上：包在 `node_modules` 里、被列进 bundles、它自己的补丁行才会被应用。
官方推荐的做法是 `plugin_manager install_bundle`，但那个工具只作用于**当前正在
运行的那个 profile**，而这里要装的目标由 `--home` / `--profile` 决定。所以按
同样的三步自己做一遍：

    1. `dsh plugin --profile <p> add file:<包目录>`   ← pnpm 装进 node_modules
    2. 把包名写进 profile 的 `dsh.profile.bundles`     ← "选中这个 bundle"
    3. 校验三处都对得上，并打印怎么让它生效

## 装到哪一份

界面既然要在 DSH 里用，插件就得在**你打开的那一份 DSH** 里。两种目标：

| 目标 | `--home` | 说明 |
|---|---|---|
| 平台自带的那一份 | 默认（`<db 目录>/dsh-home`） | 与电脑上的 DSH 完全隔离，验证与试用用这个 |
| **电脑上你正在用的那一份** | `%USERPROFILE%\\.dsh`（配 `--profile desktop`） | 装完刷新页面，左边就多一格「平台」 |

**动别人那份 DSH 之前先把 profile 的 `package.json` 备份一份**（`package.json.bak-<时间戳>`），
并在结尾打印撤销命令——装插件会改那个文件与 lock，而它同时装着使用者别的插件。

环境变量仍按目标 home 显式设置，并剥掉继承来的 `DSH_PROFILE*` / `DSH_SESSION_ID`
等身份变量（与 `dsh_runtime.LEAKED_ENV` 同一份清单）——否则一个跑在 DSH 里的会话
执行它，`dsh plugin` 可能把改动落到**发起它的那个 profile**上，而不是参数指定的那个。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import dsh_runtime                                 # noqa: E402
from pentestd.dshplugins import PLATFORM_HOME_DIR                # noqa: E402

PACKAGE_NAME = "dsh-pentest-console"
#: 装到哪个 profile：DSH 的 web 表层用 `web`，桌面应用用 `desktop`。
#: 这个默认值只影响"目标 profile 不存在时怎么提示"，装进哪个由 --profile 决定。
DEFAULT_PROFILE = "web"
DEFAULT_PACKAGE_DIR = os.path.join(ROOT, "dsh-plugins", PACKAGE_NAME)
DEFAULT_DB = os.path.join(ROOT, "data", "pentest.db")
INSTALL_TIMEOUT_S = 900


def home_for(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)),
                        PLATFORM_HOME_DIR)


def profile_dir(home: str, profile: str) -> str:
    return os.path.join(home, "profiles", profile)


def read_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path: str, payload) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def ensure_bundle(manifest_path: str, package: str) -> str:
    """把包名写进 `dsh.profile.bundles`；返回 'added' / 'present' / 'no-manifest'。"""
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        return "no-manifest"
    profile = manifest.setdefault("dsh", {}).setdefault("profile", {})
    bundles = profile.setdefault("bundles", [])
    if package in bundles:
        return "present"
    bundles.append(package)
    write_json(manifest_path, manifest)
    return "added"


def ensure_dependency(manifest_path: str, package: str, spec: str) -> str:
    """把依赖条目写进 `package.json`（`--copy` 那条不跑 pnpm 的路需要它）。

    与 `ensure_bundle` 一样只改那一处：同一个文件里装着使用者别的插件，重写整个
    `dependencies` 等于把它们一起改掉。
    """
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        return "no-manifest"
    deps = manifest.setdefault("dependencies", {})
    if deps.get(package) == spec:
        return "present"
    deps[package] = spec
    write_json(manifest_path, manifest)
    return "updated"


def copy_package(package_dir: str, profile_path: str, package: str) -> str:
    """不跑 pnpm 的安装路径：把包整棵拷进 profile 的 `node_modules`。

    **为什么要有这条路。** 在一份**正被 DSH 打开着**的 profile 上跑
    `dsh plugin add`，那条命令会去启动一个完整的桌面宿主进程来做这件事
    （`dsh-desktop-host/lib/cli.js`），而那个宿主：
      * 会一直活着（我实测 20 分钟不退），占住用户的 stdout 管道；
      * 拿着 `package.json.lock`（里面写着它的 pid），于是后续的插件管理动作被挡住。
    这次它把包与依赖写好了、却没退，bundles 那一步是补上去的。对一个 `file:` 依赖，
    pnpm 真正做的事就是把包放进 `node_modules` —— 那就自己做，一条命令两秒钟，
    不起第二个宿主、不碰锁。
    """
    target = os.path.join(profile_path, "node_modules", package)
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copytree(package_dir, target,
                    ignore=shutil.ignore_patterns(".git", "node_modules"))
    return target


def child_env(home: str) -> dict:
    env = dict(os.environ)
    env["DSH_HOME"] = home
    for key in dsh_runtime.LEAKED_ENV:
        env.pop(key, None)
    # 项目自带的 pnpm（`dsh plugin add` 靠它在 PATH 上干活）。
    dsh_runtime.prepend_vendor_bin(env)
    return env


def backup_manifest(manifest_path: str) -> str:
    """动别人那份 profile 之前先留一份原件；返回备份路径（失败返回空串）。

    装插件会改 `package.json`（依赖 + bundles），而那一份里同时装着使用者
    别的插件——一个能一条命令撤销的改动，才有资格被叫做"可以放心试"。
    """
    if not os.path.isfile(manifest_path):
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = "%s.bak-%s" % (manifest_path, stamp)
    try:
        shutil.copyfile(manifest_path, target)
        return target
    except OSError:
        return ""


def home_label(home: str, own_home: str) -> str:
    same = os.path.normcase(os.path.abspath(home)) == os.path.normcase(
        os.path.abspath(own_home))
    return "平台自带的那一份（与电脑上的 DSH 隔离）" if same else "**电脑上你正在用的那一份**"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--home", default="", help="默认 <db 所在目录>/dsh-home")
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--package-dir", default=DEFAULT_PACKAGE_DIR)
    parser.add_argument("--no-install", action="store_true",
                        help="不跑 pnpm（只在需要补 bundles 条目时用）")
    parser.add_argument("--copy", action="store_true",
                        help="不跑 pnpm：把包直接拷进 node_modules 并写两处清单"
                             "（不起第二个 DSH 宿主、不碰 profile 锁）")
    args = parser.parse_args(argv)

    home = args.home or home_for(args.db)
    profile_path = profile_dir(home, args.profile)
    manifest_path = os.path.join(profile_path, "package.json")
    package_dir = os.path.abspath(args.package_dir)

    print("DSH home  : %s" % home)
    print("            %s" % home_label(home, home_for(args.db)))
    print("profile   : %s" % args.profile)
    print("包目录    : %s" % package_dir)
    if not os.path.isdir(package_dir):
        print("包目录不存在。")
        return 2
    if not os.path.isfile(manifest_path):
        print("profile 还没初始化：先用 DSH 打开一次那个 profile（第一次会建目录），")
        print("或者 `dsh plugin add file:<包目录> --profile %s`。" % args.profile)
        return 3

    backup = backup_manifest(manifest_path)
    print("备份      : %s" % (backup or "（没能备份，继续；上面这份东西可手工还原）"))

    dsh = dsh_runtime.locate_dsh()
    if args.copy:
        # 不跑 pnpm：拷贝 + 两处清单，全部由这个脚本自己做。见 `copy_package` 的理由。
        target = copy_package(package_dir, profile_path, PACKAGE_NAME)
        print("拷贝      : %s" % target)
        spec = "file:%s" % package_dir.replace(os.sep, "/")
        print("依赖条目  : %s" % ensure_dependency(manifest_path, PACKAGE_NAME, spec))
    elif not args.no_install:
        if not dsh:
            print("找不到 dsh 启动器（DSH 运行时）；要么装 DSH，要么用 --copy / --no-install。")
            return 4
        print("dsh       : %s" % dsh)
        spec = "file:%s" % package_dir
        # `dsh` 可能是 `.js` 入口（项目内那份）→ 得先给 node；
        # `.cmd` 则得给 cmd.exe。两者都在 command_for 里。
        command = dsh_runtime.command_for(dsh) + ["plugin", "--profile",
                                                   args.profile, "add", spec]
        print("安装      : %s" % " ".join(command))
        try:
            done = subprocess.run(command, cwd=profile_path,
                                  env=child_env(home), capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=INSTALL_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            print("安装超时（%ds）。" % INSTALL_TIMEOUT_S)
            return 5
        print("退出码    : %s" % done.returncode)
        for stream, text in (("stdout", done.stdout), ("stderr", done.stderr)):
            tail = (text or "").strip().splitlines()[-12:]
            if tail:
                print("--- %s ---" % stream)
                for line in tail:
                    print("  " + line)
        if done.returncode != 0:
            print("安装失败：上面的输出是原文，没有截断原因。")
            return 6

    state = ensure_bundle(manifest_path, PACKAGE_NAME)
    print("bundles   : %s" % state)

    installed = os.path.isdir(os.path.join(profile_path, "node_modules",
                                           PACKAGE_NAME))
    manifest = read_json(manifest_path) or {}
    deps = (manifest.get("dependencies") or {})
    bundles = (((manifest.get("dsh") or {}).get("profile") or {})
               .get("bundles") or [])
    marker = read_json(os.path.join(profile_path, "node_modules", PACKAGE_NAME,
                                    "package.json")) or {}
    print("node_modules<%s>: %s" % (PACKAGE_NAME, installed))
    print("依赖条目  : %s" % deps.get(PACKAGE_NAME, "（无）"))
    print("在 bundles: %s" % (PACKAGE_NAME in bundles))
    print("包版本    : %s" % marker.get("version", "?"))

    ok = installed and PACKAGE_NAME in bundles and PACKAGE_NAME in deps
    print("三处一致  : %s" % ("是" if ok else "**否**（上面逐条列出差异）"))
    print()
    own = os.path.normcase(os.path.abspath(home)) == os.path.normcase(
        os.path.abspath(home_for(args.db)))
    if own:
        print("让它生效  : 用 DSH 打开一次那个 profile（它只在加载 profile 时读插件）")
    else:
        print("让它生效  : 刷新你正在用的那个 DSH 页面（Ctrl+F5）。")
        print("            补丁层是热生效的，但浏览器要先拿到新的客户端模块；")
        print("            刷新之后左侧栏会多出一格「平台」，设置页会多一节「渗透测试平台」。")
    print("配令牌    : python -m pentestd console-token      （把输出粘进那一格设置里一次）")
    print("放行来源  : python -m pentestd access add-origin http://<你打开 DSH 的那个源>")
    print("撤销      : %s plugin --profile %s remove %s"
          % (dsh or "<dsh>", args.profile, PACKAGE_NAME))
    if backup:
        print("            （profile 清单的原件在 %s）" % backup)
    return 0 if ok else 7


if __name__ == "__main__":
    sys.exit(main())
