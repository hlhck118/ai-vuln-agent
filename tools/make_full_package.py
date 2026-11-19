#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全量打包：把项目（含 data/tools/skills/db/密钥/设置/DSH node_modules）打成 zip。

与 make_handoff_zip.py（干净交接，白名单排除运行期数据）不同，本脚本是**镜像打包**：
几乎收录一切，只排除真正无用的运行期残留。用户诉求「清除无用文件后，包含所有数据、
工具、技能、设置等打包到桌面」。

排除项（都是重建物或运行期垃圾）：
  * __pycache__ / *.pyc / *.lock / *.log / *.tmp  —— 已在前置清理中删除，这里兜底再排
  * 桌面本脚本自身的输出（避免递归）
  * 目标 zip 自身
"""
import argparse
import datetime as dt
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根

# 始终排除：重建物 + 运行期垃圾 + 已知无用
EXCLUDE_DIR_NAMES = {
    "__pycache__",
    ".git",
    ".venv",
    "venv",
}
EXCLUDE_SUFFIXES = (
    ".pyc", ".pyo", ".lock", ".log", ".tmp",
)

def desktop_dir() -> str:
    import ctypes.wintypes
    buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
    ctypes.windll.shell32.SHGetFolderPathW(None, 0, None, 0, buf)  # 0 = CSIDL_DESKTOP
    return buf.value

def excluded(rel: str) -> str:
    """返回排除原因，空串表示保留。"""
    parts = rel.replace("\\", "/").split("/")
    if any(p in EXCLUDE_DIR_NAMES for p in parts):
        return "目录 %s" % next(p for p in parts if p in EXCLUDE_DIR_NAMES)
    if rel.endswith(EXCLUDE_SUFFIXES):
        return "后缀 %s" % os.path.splitext(rel)[1]
    # 排除本脚本自身的输出与已知无用的工具临时物
    if parts and parts[-1] in ("make_full_package.py",):
        return "打包脚本自身"
    return ""

def collect():
    included, skipped = [], []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # 跳过被排除的目录
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIR_NAMES]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT).replace("\\", "/")
            if rel.startswith("data/") is False and rel.startswith("tools/make_full_package") is False:
                pass
            reason = excluded(rel)
            (skipped if reason else included).append((rel, reason, full))
    return sorted(included, key=lambda x: x[0]), sorted(skipped, key=lambda x: x[0])

def main():
    ap = argparse.ArgumentParser(description="全量镜像打包")
    ap.add_argument("--out", default=None, help="输出 zip 路径（默认桌面）")
    ap.add_argument("--root-in-zip", default="ai-vuln-agent", help="包内根目录名")
    args = ap.parse_args()

    out = args.out or os.path.join(desktop_dir(), "pentest-platform-全量-%s.zip"
                                   % dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    included, skipped = collect()
    print("收录 %d 个文件，排除 %d 项" % (len(included), len(skipped)))
    total = sum(os.path.getsize(f) for _, _, f in included)
    print("预计体积 %.1f MB" % (total / 1024 / 1024))

    staging = out + ".tmp"
    if os.path.exists(staging):
        os.remove(staging)
    with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for rel, _, full in included:
            arcname = "%s/%s" % (args.root_in_zip, rel)
            zf.write(full, arcname)
    os.replace(staging, out)
    print("已写入 %s (%.1f MB)" % (out, os.path.getsize(out) / 1024 / 1024))

    # 校验
    with zipfile.ZipFile(out) as zf:
        bad = zf.testzip()
        names = zf.namelist()
    print("zip 完整性: %s" % ("OK" if bad is None else "损坏项 %s" % bad))
    print("包内文件数 %d" % len(names))
    # 关键项抽查
    for k in ["data/pentest.db", "data/secret.key", "data/skills/", "data/tools/",
              "data/dsh-home/profiles/", "vendor/dsh/node_modules/", "start.sh"]:
        hit = [n for n in names if n.endswith("/" + k) or k in n and (k.endswith("/") or n.split("/")[-1] == k.split("/")[-1])]
        print("  关键项 %-30s -> %s" % (k, "有 %d 个" % len(hit) if hit else "缺"))
    return 0

if __name__ == "__main__":
    sys.exit(main())
