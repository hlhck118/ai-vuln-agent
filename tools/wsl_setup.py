#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一份 Linux 发行版导进项目目录，供 `toolinstall` 的 wsl 渠道使用。

    python tools/wsl_setup.py              # 用默认的 Ubuntu WSL rootfs
    python tools/wsl_setup.py --status     # 只报告现在是什么状态
    python tools/wsl_setup.py --rootfs X   # 用本地已有的 rootfs 文件
    python tools/wsl_setup.py --force      # 已存在时先注销再重导

装完之后 wsl 渠道才可用（对话里说「装上 hydra」之类）。

## 发行版落在项目目录里

    <项目>/data/tools/wsl/<发行版>/ext4.vhdx

**落在项目目录里是这条渠道唯一的存在理由**：整份项目拷到另一台机器（那台机器
要有 WSL），发行版跟着走；删掉那个目录，工具就没了，宿主机上不多任何东西。

## 为什么默认不是 Kali

Kali 的 `cloud-genericcloud` 镜像里只有**一个 `disk.raw`**（一张磁盘镜像），而
`wsl --import` 要的是 rootfs 文件树——实测被它拒（`WSL_E_NOT_A_LINUX_DISTRO`，
「导入的文件不是有效的 Linux 分发」）。Ubuntu 另外发一份 WSL 专用 rootfs
（文件名里就写着 `wsl.rootfs`），格式对得上。

而工具层面这个选择不吃亏：hydra / ncrack / smbclient 走 apt、netexec 走 pipx、
evil-winrm 走 gem、metasploit 走它自己的官方 apt 源——都是各自的**正规**形态，
Kali 只是替你打包过一遍。

## 校验

下载完从官方 `SHA256SUMS` 取那一行比对。**这不是形式**：实测这条链路上的下载
会在 80% 处被服务器单方面切断（`schannel: server closed abruptly`），而
`curl` 的退出码是 56、文件却是"看起来差不多大"的 180.4 MB / 223.2 MB——按大小
判断会得到一个**看起来装好了**的坏归档。
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import toolinstall as T            # noqa: E402

_BASE = "https://cloud-images.ubuntu.com/wsl/releases/24.04/current/"
DEFAULT_URL = _BASE + "ubuntu-noble-wsl-amd64-wsl.rootfs.tar.gz"
DEFAULT_SUMS = _BASE + "SHA256SUMS"


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _expected_digest(sums_url: str, name: str) -> str:
    """从官方 SHA256SUMS 取这一行的摘要。**取不到就返回空串并说出来。**"""
    if not sums_url:
        return ""
    try:
        got = T.default_fetch(sums_url, None, 60.0, 1 << 20)
    except Exception as exc:                     # noqa: BLE001 - 网络
        print("  取 SHA256SUMS 失败（%s），跳过校验。" % exc)
        return ""
    text = str(got.get("text") or "")
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[-1].lstrip("*") == name:
            return fields[0].lower()
    print("  SHA256SUMS 里没有 %s 那一行，跳过校验。" % name)
    return ""


def _download(url: str, dest: str) -> bool:
    """下载 rootfs。优先用 curl——它支持续传，而这条链路上断流是常态。"""
    curl = shutil.which("curl")
    if curl:
        print("  用 curl 下载（断流可续传）")
        done = subprocess.run(
            [curl, "-L", "-C", "-", "--retry", "6", "--retry-delay", "3",
             "--max-time", "1800", "-o", dest, url],
            capture_output=True)
        return done.returncode == 0
    print("  没有 curl，改用内置的取数器（不支持续传）")
    got = T.default_fetch(url, dest, 1800.0, 1 << 31)
    return bool(got.get("ok"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="把一份 Linux 发行版导进项目目录")
    parser.add_argument("--distro", default="",
                        help="发行版名（默认取 toolinstall 的那一个）")
    parser.add_argument("--url", default=DEFAULT_URL, help="rootfs 下载地址")
    parser.add_argument("--sums", default=DEFAULT_SUMS, help="SHA256SUMS 地址")
    parser.add_argument("--rootfs", default="",
                        help="用本地已有的 rootfs 文件（跳过下载与校验）")
    parser.add_argument("--force", action="store_true",
                        help="已存在时先注销再重导（**发行版里装过的工具会一起没掉**）")
    parser.add_argument("--status", action="store_true", help="只报告状态")
    args = parser.parse_args(argv)

    distro = (args.distro or "").strip() or T.wsl_distro()
    exe = T.wsl_exe()
    present = T.wsl_listed(distro)
    target = os.path.join(T.tools_root(), "wsl", distro)

    print("wsl.exe       : %s" % (exe or "（没找到）"))
    print("发行版        : %s" % distro)
    print("目标目录      : %s" % target)
    print("现在导入了吗  : %s" % ("是" if present else "否"))

    if args.status:
        if present:
            got = T.wsl_run(["sh", "-c", "head -2 /etc/os-release"],
                            timeout=60.0, distro=distro)
            print("发行版自述    : %s"
                  % (got.get("output") or "").replace("\n", " | "))
            found = T.wsl_inventory(refresh=True)
            print("发行版里的工具: %s"
                  % ("、".join(sorted(found)) or "（一个都没有）"))
        return 0

    if not exe:
        print("\n没有 wsl.exe。以管理员执行 `wsl --install`，或在「启用或关闭"
              "Windows 功能」里勾上「适用于 Linux 的 Windows 子系统」。")
        return 2
    if present and not args.force:
        print("\n已经导入了，什么也没做。要重来请加 --force"
              "（会先注销这个发行版，**里面装过的工具会一起没掉**）。")
        return 0

    source = args.rootfs
    if source:
        if not os.path.isfile(source):
            print("\n本地 rootfs 不存在：%s" % source)
            return 2
        print("\n用本地 rootfs：%s" % source)
    else:
        source = os.path.join(T.tools_root(), "_dl", os.path.basename(args.url))
        os.makedirs(os.path.dirname(source), exist_ok=True)
        print("\n下载 rootfs → %s" % source)
        if not _download(args.url, source):
            print("  下载失败。可以手工下好再用 --rootfs 指过来。")
            return 1
        print("  完成：%.1f MB" % (os.path.getsize(source) / 1048576.0))

    if args.rootfs:
        print("\n跳过校验（--rootfs 是你自己给的文件，没有可比对的官方摘要）。")
    else:
        expected = _expected_digest(args.sums, os.path.basename(args.url))
        got = _sha256(source)
        print("  sha256      : %s" % got)
        if expected:
            print("  官方摘要    : %s" % expected)
            if got != expected:
                print("\n摘要不一致——这个文件不是官方发布的那一份，已中止。"
                      "\n删掉重下：%s" % source)
                return 1
            print("  校验        : 通过")
        else:
            print("  **没有可比对的官方摘要**，请自行确认这个文件可信。")

    if present and args.force:
        print("\n注销旧的 %s …" % distro)
        subprocess.run([exe, "--unregister", distro], check=False,
                       capture_output=True)

    print("导入到 %s …" % target)
    done = subprocess.run([exe, "--import", distro, target, source],
                          capture_output=True,
                          env=dict(os.environ, WSL_UTF8="1"))
    if done.returncode != 0:
        text = (done.stdout or b"").decode("utf-8", "replace")
        text += (done.stderr or b"").decode("utf-8", "replace")
        print("\n导入失败（退出码 %s）：\n%s" % (done.returncode, text.strip()))
        print("\n一句常被忽略的：rootfs 必须是**文件树**（`tar -tf` 里能看到 "
              "etc/ usr/ bin/）。里面只有一个 disk.raw 的云镜像装不进来。")
        return 1

    print("\n导入成功。")
    T.wsl_forget()
    got = T.wsl_run(["sh", "-c", "head -2 /etc/os-release"], timeout=60.0,
                    distro=distro)
    print("  发行版自述 : %s" % (got.get("output") or "").replace("\n", " | "))
    print("  vhdx       : %s" % target)
    print("\n下一步：对话里说「装上 hydra」即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
