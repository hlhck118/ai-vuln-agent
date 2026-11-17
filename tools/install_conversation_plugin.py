# -*- coding: utf-8 -*-
"""把某个 DSH 插件装进**平台对话所用的那份 profile**。

平台对话开着「DSH 运行环境」（设置里的 `dsh_runtime=sdk`）时，每一轮都由项目
自带的 DSH（`vendor/dsh`）跑；它加载的是 `data/dsh-home` 里 `dsh_profile` 指定的
那个 profile。插件只有装进**这个** profile，才出现在对话的工具清单里 —— 装进平台
自己的插件容器（`pentest-platform`）是给「插件」页签用的另一回事。

三步与 DSH 自己的做法一致（少一步补丁行就不会被应用）：

    1. `dsh plugin --profile <p> add <spec>`   ← pnpm 装进 profile 的 node_modules
    2. profile 的 `dsh.profile.bundles` 里有它  ← "选中这个 bundle"
    3. 包目录里的 `cordis.patch.yml` 存在        ← 它自己的挂载行才有东西可应用

第 2 步通常由 DSH 的 plugin-manager 在安装后自动补（它会把写进 profile 的依赖
reconcile 进 bundles）；本脚本装完再自己核一遍，缺了就直接补上，免得"装上了但
没生效"这种最难查的状态。

用法：

    python -X utf8 tools/install_conversation_plugin.py            # 装/检查默认包
    python -X utf8 tools/install_conversation_plugin.py --check     # 只看状态，不动
    python -X utf8 tools/install_conversation_plugin.py --spec @nanmicoder/dsh-agent-teams@0.1.25

默认包是团队的「子智能体」插件 `@nanmicoder/dsh-agent-teams`：它只 insert 自己的
`agent-teams` 行、不禁用原生 `subagent`，所以与平台既有口径（模型用原生 subagent）
叠加而不是替换。装完**下一轮对话**就有效果，不用重启平台服务（profile 在每次
`channel` 打开时读）；但正在跑的那一轮不会中途长出工具。
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import dsh_runtime                     # noqa: E402
from pentestd.store import Store                     # noqa: E402

DEFAULT_DB = os.path.join(ROOT, "data", "pentest.db")
DEFAULT_SPEC = "@nanmicoder/dsh-agent-teams"
INSTALL_TIMEOUT_S = 900


def package_name(spec: str) -> str:
    """`@scope/name@1.2.3` → `@scope/name`。"""
    spec = spec.strip()
    if spec.startswith("@"):
        head, _, tail = spec.partition("/")
        return head + "/" + tail.split("@", 1)[0]
    return spec.split("@", 1)[0]


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


def profile_dir(home: str, profile: str) -> str:
    return os.path.join(home, "profiles", profile)


def status(home: str, profile: str, name: str) -> dict:
    """三处都对得上才算可用：依赖里有、bundles 里有、补丁文件在。"""
    folder = profile_dir(home, profile)
    manifest = read_json(os.path.join(folder, "package.json"), {}) or {}
    deps = (manifest.get("dependencies") or {})
    profile_block = manifest.get("dsh", {}).get("profile", {}) or {}
    bundles = profile_block.get("bundles") or []
    installed = os.path.isdir(os.path.join(folder, "node_modules", *name.split("/")))
    patch = os.path.join(folder, "node_modules", *name.split("/"), "cordis.patch.yml")
    return {
        "home": home,
        "profile": profile,
        "folder": folder,
        "spec": name,
        "declared": deps.get(name, ""),
        "installed": installed,
        "bundled": name in bundles,
        "patch": patch if os.path.isfile(patch) else "",
        "bundles": bundles,
    }


def usable(info: dict) -> bool:
    return bool(info["declared"] and info["installed"]
                and info["bundled"] and info["patch"])


def conversation_profile(store: Store) -> str:
    return (store.get_setting(dsh_runtime.SETTING_PROFILE, "") or "").strip() \
        or dsh_runtime.DEFAULT_PROFILE


def recorded_store(folder: str) -> str:
    """profile 的 node_modules 是在哪个 pnpm store 里链的。

    pnpm 换了 store 就不肯再往这份 node_modules 里装（“pnpm now wants to use the
    store at …”），而 store 位置来自环境/用户配置，不来自这个 profile。把已记录的
    store 显式传给子进程，换机/换终端后就不会因为“谁也说不清当时用的哪个 store”
    而装不上；找不到记录（全新 profile）就返回空串，交给 pnpm 自己决定。
    """
    path = os.path.join(folder, "node_modules", ".modules.yaml")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return ""
    # 行形如：  "storeDir": "X:\\pnpm-store\\v11",   —— 键带引号，值里的反斜杠是转义过的
    match = re.search(r'^\s*"?storeDir"?\s*:\s*"(.+?)"\s*,?\s*$', text, re.M)
    if not match:
        return ""
    path = match.group(1).replace("\\\\", "\\")
    return path if os.path.isdir(path) else ""


def moved_profile(folder: str) -> str:
    """profile 是不是被从另一个路径/盘搬过来的。

    pnpm 把 virtual store 的**绝对路径**记在 `node_modules/.modules.yaml` 里；把一份
    home 整个拷到别处后，pnpm 会拒绝继续往这份 node_modules 里装（“pnpm now wants to
    use the virtual store at …”）。这不是本脚本能改的配置，而是“要重新装一遍依赖”
    的信号：返回记录的旧路径（与当前不一致时），一致则返回空串。
    """
    path = os.path.join(folder, "node_modules", ".modules.yaml")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return ""
    match = re.search(r'^\s*"?virtualStoreDir"?\s*:\s*"(.+?)"\s*,?\s*$', text, re.M)
    if not match:
        return ""
    recorded = match.group(1).replace("\\\\", "\\")
    expected = os.path.join(folder, "node_modules", ".pnpm")
    return "" if os.path.normcase(recorded) == os.path.normcase(expected) else recorded


def install(home: str, profile: str, spec: str) -> int:
    dsh = dsh_runtime.locate_dsh()
    if not dsh:
        print("[dsh] 找不到项目自带的 DSH（vendor/dsh）。先跑 deploy 的安装步骤。")
        return 2
    stale = moved_profile(profile_dir(home, profile))
    if stale:
        print("[dsh] 这份 profile 是从别处/别的盘搬过来的（node_modules 记着 %s）。" % stale)
        print("[dsh] pnpm 不接受跨路径续装。先删掉这两处再重跑本脚本："
              "%s 与 %s" % (os.path.join(profile_dir(home, profile), "node_modules"),
                             os.path.join(profile_dir(home, profile), "pnpm-lock.yaml")))
        return 5
    argv = dsh_runtime.command_for(dsh) + ["plugin", "--profile", profile, "add", spec]
    env = dsh_runtime.DshRuntime(home=home, root=ROOT, profile=profile).env()
    env.setdefault("CI", "1")            # pnpm 的交互提示在无 TTY 时会卡住
    store = recorded_store(profile_dir(home, profile))
    if store:
        # pnpm 的默认 store 跟着**项目所在盘**走（同一份 profile 换到别的盘一装就报
        # “pnpm now wants to use the store at …”）。环境变量要带 `pnpm_` 前缀才生效，
        # `npm_` 那份是给 npm 的；两个都写，宁可重复。
        env["pnpm_config_store_dir"] = store
        env["npm_config_store_dir"] = store
        print("[dsh] pnpm store=%s" % store)
    print("[dsh] " + " ".join(argv))
    try:
        done = subprocess.run(argv, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=INSTALL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        print("[dsh] 安装超时（%ds）：pnpm 可能在等交互输入。" % INSTALL_TIMEOUT_S)
        return 3
    text = done.stdout.decode("utf-8", "replace") if done.stdout else ""
    for line in text.splitlines()[-8:]:
        print("  " + line)
    if done.returncode:
        print("[dsh] 安装失败：exit %d" % done.returncode)
        return done.returncode
    name = package_name(spec)
    info = status(home, profile, name)
    if not info["bundled"]:
        # plugin-manager 通常会自己补；它没补（旧版/被手改过）就补上，否则装=没装。
        path = os.path.join(info["folder"], "package.json")
        manifest = read_json(path, {}) or {}
        block = manifest.setdefault("dsh", {}).setdefault("profile", {})
        bundles = list(block.get("bundles") or [])
        if name not in bundles:
            bundles.append(name)
            block["bundles"] = bundles
            write_json(path, manifest)
            print("[dsh] 已把它补进 dsh.profile.bundles")
        info = status(home, profile, name)
    return 0 if usable(info) else 4


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=DEFAULT_DB, help="平台数据库（读 dsh_profile）")
    parser.add_argument("--home", default="", help="DSH home，默认数据库同级的 dsh-home")
    parser.add_argument("--profile", default="", help="profile 名，默认读设置 dsh_profile")
    parser.add_argument("--spec", default=DEFAULT_SPEC,
                        help="包名或 name@version，默认 " + DEFAULT_SPEC)
    parser.add_argument("--check", action="store_true", help="只报告状态，不安装")
    args = parser.parse_args(argv)

    home = os.path.abspath(args.home) if args.home else \
        os.path.join(os.path.dirname(os.path.abspath(args.db)), "dsh-home")
    profile = args.profile
    if not profile:
        try:
            store = Store(args.db)
            profile = conversation_profile(store)
            store.close()
        except Exception as exc:                     # 库不在也能装，只要给了 --profile
            print("[dsh] 读不到设置（%s），用默认 profile" % exc)
            profile = dsh_runtime.DEFAULT_PROFILE
    name = package_name(args.spec)
    before = status(home, profile, name)
    print("[dsh] home=%s profile=%s package=%s" % (home, profile, name))
    print("[dsh] 现状：declared=%r installed=%s bundled=%s"
          % (before["declared"], before["installed"], before["bundled"]))
    if args.check:
        print("[dsh] 可用：%s" % ("是" if usable(before) else "否"))
        return 0 if usable(before) else 1
    if usable(before):
        print("[dsh] 已经装好了；要换版本请带 --spec name@version。")
        return 0
    code = install(home, profile, args.spec)
    after = status(home, profile, name)
    print("[dsh] 装完：declared=%r bundle=%s patch=%s"
          % (after["declared"], after["bundled"], bool(after["patch"])))
    print("[dsh] 可用：%s（下一轮对话生效；正在跑的那一轮不会中途长出工具）"
          % ("是" if usable(after) else "否"))
    return code


if __name__ == "__main__":
    sys.exit(main())
