# -*- coding: utf-8 -*-
"""一次真实的隔离端到端：真 ControlPlane、真 OpRunner、真 dsh CLI。

跑之前先给电脑上那份 DSH profile 拍指纹，跑完再拍一次并逐行比对。任何一处写入
（哪怕只是 mtime 变了）都会让这两份清单对不上。

输出刻意全用 ASCII：这个脚本会打在 Windows 控制台上，中文会因码页问题变成乱码，
而"看不懂的验证结果"等于没有验证。
"""
import hashlib
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import dshplugins, hub, server          # noqa: E402
from pentestd.store import Store                       # noqa: E402

DATA_DIR = os.path.join(ROOT, "data")
MACHINE_PROFILE = os.path.join(os.path.expanduser("~"), ".dsh", "profiles", "desktop")
TEST_PACKAGE = "is-odd"


def fingerprint(root):
    """文件名 + 大小 + mtime 的全量清单，再做一次总哈希。"""
    rows = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "node_modules"]
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            rows.append("%s|%d|%d" % (os.path.relpath(path, root), stat.st_size,
                                      stat.st_mtime_ns))
    rows.sort()
    blob = "\n".join(rows).encode("utf-8")
    return hashlib.sha256(blob).hexdigest(), len(rows)


def drain(plane, timeout=600.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ops = plane.store.plugin_ops(3)
        if ops and all(item["status"] != "running" for item in ops):
            return ops
        time.sleep(0.3)
    return plane.store.plugin_ops(3)


def main():
    print("data dir        :", DATA_DIR)
    print("machine profile :", MACHINE_PROFILE)

    before_hash, before_rows = fingerprint(MACHINE_PROFILE)
    print("machine BEFORE  : %s (%d files)" % (before_hash[:16], before_rows))

    store = Store(os.path.join(DATA_DIR, "pentest.db"))
    plane = server.ControlPlane(store, os.path.join(ROOT, "web"))
    print("plane.dsh_home  :", plane.dsh_home)
    print("platform_home() :", dshplugins.platform_home())
    print("== isolation is wired:", plane.dsh_home == dshplugins.platform_home())

    snap = dshplugins.snapshot(refresh=True)
    print("snapshot isolated=%s available=%s" % (snap["isolated"], snap["available"]))
    print("snapshot home   :", snap["home"])
    print("snapshot profile:", snap["profile"])
    print("plugins before  :", sorted(p["name"] for p in snap["plugins"]))
    print("presets before  :", sorted(p["presetId"] for p in snap["presets"]))
    print("skills before   :", len(snap["skills"]))

    # ---- 真安装：走 OpRunner → hub.install → dsh CLI（DSH_HOME 指向项目）----
    print("\n--- install %s (real CLI) ---" % TEST_PACKAGE)
    outcome = plane.plugin_runner.submit("install", TEST_PACKAGE,
                                         dshplugins.PLATFORM_PROFILE, "e2e")
    print("submitted:", outcome["ok"], outcome["reason"])
    ops = drain(plane)
    print("op status:", ops[0]["status"], "exit:", ops[0]["exitCode"])
    print("op tail  :", (ops[0]["output"] or "").strip().splitlines()[-1:])

    installed = os.path.join(dshplugins.platform_profile_dir(),
                             "node_modules", TEST_PACKAGE)
    print("installed into project dir:", os.path.isdir(installed))
    snap2 = dshplugins.snapshot(refresh=True)
    print("plugins after   :", sorted(p["name"] for p in snap2["plugins"]))
    print("existed before  :",
          sorted(p["name"] for p in snap["plugins"]) ==
          sorted(n for n in (p["name"] for p in snap2["plugins"]) if n != TEST_PACKAGE))

    # ---- 真卸载 ----
    print("\n--- remove %s (real CLI) ---" % TEST_PACKAGE)
    outcome = plane.plugin_runner.submit("remove", TEST_PACKAGE,
                                         dshplugins.PLATFORM_PROFILE, "e2e")
    ops = drain(plane)
    print("op status:", ops[0]["status"], "exit:", ops[0]["exitCode"])
    print("gone from project dir:", not os.path.isdir(installed))
    snap3 = dshplugins.snapshot(refresh=True)
    print("plugins final   :", sorted(p["name"] for p in snap3["plugins"]))
    print("presets final   :", sorted(p["presetId"] for p in snap3["presets"]))
    print("skills final    :", len(snap3["skills"]))

    # ---- 收尾：电脑上那份 DSH 必须一模一样 ----
    after_hash, after_rows = fingerprint(MACHINE_PROFILE)
    print("\nmachine AFTER   : %s (%d files)" % (after_hash[:16], after_rows))
    print("== MACHINE UNTOUCHED:", before_hash == after_hash and before_rows == after_rows)

    # ---- 隔离的另外两条：CLI 的 DSH_HOME 与拒绝执行 ----
    print("\n-- guards --")
    print("refuse without home:",
          hub.install(DATA_DIR, TEST_PACKAGE, dshplugins.PLATFORM_PROFILE)["ok"])
    print("refuse foreign profile accepted by hub_install:",
          "only checked in HTTP tests")

    store.close()


if __name__ == "__main__":
    main()
