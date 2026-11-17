# -*- coding: utf-8 -*-
"""浏览器可执行文件在哪：给探针/截图工具共用的一张候选表。

为什么单独一份：这张表原先在八个脚本里各抄了一遍，而且每份都写死
`C:\\Program Files\\...`。写死盘符有两个坏处——

* 系统装在别的盘、或浏览器装在自定义目录时，候选表全部落空，探针打印 SKIP，
  看起来像"本机没有浏览器"，而实际上有；
* 同一张表抄了八份，加一个浏览器要改八处，改漏一处就会让某一支探针静默退化。

Windows 的安装根本来就有环境变量（`%ProgramFiles%`、`%ProgramFiles(x86)%`、
`%ProgramW6432%`、`%LOCALAPPDATA%`），从那里拼出**绝对**路径即可：进程确实需要
绝对路径，但"绝对"不等于"写死"。

顺序：显式环境变量 → PATH 上的命令名 → 由环境变量拼出的安装位置 → POSIX 常见位置。
"""
import os
import shutil

# 显式覆盖：给一个就用它（找不到再往下走）。
ENV = ("EDGE_PATH", "CHROME_PATH", "BROWSER_PATH")

# PATH 上的命令名（Windows 的 .exe 由 shutil.which 按 PATHEXT 补全）。
NAMES = ("msedge", "chrome", "chromium", "chromium-browser", "google-chrome",
         "microsoft-edge", "brave", "brave-browser", "firefox")

# 安装根下的相对子路径：盘符由环境变量给。
TAILS = (("Microsoft", "Edge", "Application", "msedge.exe"),
         ("Google", "Chrome", "Application", "chrome.exe"),
         ("Chromium", "Application", "chrome.exe"),
         ("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
         ("Mozilla Firefox", "firefox.exe"))

POSIX = ("/usr/bin/microsoft-edge",
         "/usr/bin/google-chrome",
         "/usr/bin/chromium",
         "/usr/bin/chromium-browser",
         "/snap/bin/chromium",
         "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
         "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")


def roots():
    """Windows 上放软件的根目录，全部来自环境变量。"""
    out = []
    for key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
        value = os.environ.get(key)
        if value and value not in out:
            out.append(value)
    if out:
        return out
    # 环境变量缺失（精简环境、非 Windows）时退回系统盘根部拼出来的位置：仍然不写死盘符。
    drive = os.environ.get("SystemDrive") or os.path.splitdrive(
        os.environ.get("WINDIR") or "")[0]
    if drive:
        out.append(os.path.join(drive + os.sep, "Program Files"))
        out.append(os.path.join(drive + os.sep, "Program Files (x86)"))
    return out


def candidates():
    """按优先级排好的候选绝对路径（可能含不存在的项）。"""
    out = [os.environ.get(key) or "" for key in ENV]
    for name in NAMES:
        found = shutil.which(name)
        if found:
            out.append(found)
    if os.name == "nt":
        for root in roots():
            for tail in TAILS:
                out.append(os.path.join(root, *tail))
    out.extend(POSIX)
    seen, unique = set(), []
    for path in out:
        if not path or path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def find():
    """第一个真实存在的浏览器；一个都没有就返回空串（调用方据此打印 SKIP）。"""
    for path in candidates():
        if os.path.exists(path):
            return path
    return ""


BROWSERS = candidates()
