#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Keep a public HTTPS tunnel to the local console alive.

Why this exists: the console binds 127.0.0.1 by default and the operator wants to
reach it from a phone or another network. Two free relay providers were measured
from this machine, and only one of them reaches the far side:

  * serveo.net      — ssh on **443**. Held 24/24 health checks over 6.5 minutes at
                      1.6-2.5s, and the console index (129 KB) in 2.8s. This is
                      the default. (An earlier session died after 200-330s with
                      "Timeout, server serveo.net not responding"; re-measured on
                      2026-11-20 and it stayed up, so that verdict is stale.)
  * localhost.run   — ssh on 22, which this link cannot reach: TCP connects, then
                      the peer closes without sending an SSH banner, and 22/443 on
                      its A records (54.161.197.247 / 35.171.254.69 /
                      54.82.85.249) answer the same way. The local resolver also
                      hands back a Clash fake-ip (198.18.10.220) for the name. Kept
                      as an option for networks where 443 is the blocked one.

Both are reached over ssh -R. The child dies on its own eventually (relay
recycles, laptop sleeps, network flaps), so this wraps it: watch the ssh child,
scrape the assigned URL out of its banner, write the URL to a file the operator
can read, and reconnect when it drops.

Usage:
    python tools/keep_tunnel.py                       # 127.0.0.1:8787 -> serveo.net
    python tools/keep_tunnel.py --provider localhost.run
    python tools/keep_tunnel.py --port 8787 --url-file data/tunnel-url.txt
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time

# 每个中转给的地址形状不一样，抓的是**最后一条**匹配（横幅里会先给文档站）。
URL_PATTERNS = {
    "serveo": re.compile(r"https://([a-z0-9-]+)\.serveousercontent\.com"),
    "localhost.run": re.compile(r"https://([a-z0-9]+)\.app\.lhr\.life"),
}

# 端口挑的是**能穿过去**的那个：serveo 走 443（这条链路上 22 连不通，见头注释），
# localhost.run 的免费通道必须用 `nokey@` 这个用户名走匿名，带 key 的组合要账号。
SSH_TARGETS = {
    "serveo": ("serveo.net", 443, "80"),
    "localhost.run": ("nokey@localhost.run", 22, "80"),
}


def build_command(provider: str, port: int, host: str, ssh: str) -> list[str]:
    target, ssh_port, remote_port = SSH_TARGETS[provider]
    return [
        ssh,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=" + os.devnull,
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=20",
        "-o", "ServerAliveCountMax=3",
        "-o", "ConnectTimeout=15",
        "-T",
        "-p", str(ssh_port),
        "-R", "%s:%s:%d" % (remote_port, host, port),
        target,
    ]


def announce(url: str, url_file: str) -> None:
    print("public url: %s" % url, flush=True)
    if not url_file:
        return
    directory = os.path.dirname(os.path.abspath(url_file))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    tmp = url_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(url + "\n")
    os.replace(tmp, url_file)


# ssh 活着、隧道已经死了的几种说法。serveo 尤其阴：它到期时**不关 ssh**，只在横幅里
# 打一句 "Port forwarding for <子域> expired"，连接照旧开着——不主动识别就会一直守着一条
# 没有出口的隧道，URL 也一直停在那个已经 502 的地址上。实测 2026-11-20 10:00 起的那次
# 转发在 11 分钟后 expired，而 ssh 进程还在。
DROP_MARKERS = (
    "expired",
    "remote port forwarding failed",
    "administratively prohibited",
    "Connection to ",
    "Broken pipe",
    "Timeout, server",
)


def run_once(cmd: list[str], url_file: str, pattern: re.Pattern) -> None:
    """跑一次 ssh，直到它自己退出、说出口子没了、或被 Ctrl+C 打断。"""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            match = pattern.search(line)
            if match:
                announce(match.group(0), url_file)
                continue
            if "Warning: Permanently added" in line:
                continue
            print(line, flush=True)
            lowered = line.lower()
            if any(marker.lower() in lowered for marker in DROP_MARKERS):
                # 口子没了就收工：外层会重连并抓到新地址。停在旧地址上比断掉更糟——
                # 界面上看着有个 URL，发出去却是 502。
                print("relay closed the forward, reconnecting", flush=True)
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="keep a public reverse tunnel alive")
    parser.add_argument("--provider", default="serveo",
                        choices=sorted(URL_PATTERNS),
                        help="中转方；默认 serveo（走 443，实测 6.5 分钟 24/24 个 200）")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--url-file", default="tunnel-url.txt")
    parser.add_argument("--retry-seconds", type=float, default=5.0)
    parser.add_argument("--ssh", default="")
    args = parser.parse_args(argv)

    ssh = args.ssh or shutil.which("ssh") or "ssh"
    cmd = build_command(args.provider, args.port, args.host, ssh)
    pattern = URL_PATTERNS[args.provider]
    print("tunnel: %s:%d -> %s" % (args.host, args.port, SSH_TARGETS[args.provider][0]),
          flush=True)
    while True:
        started = time.time()
        try:
            run_once(cmd, args.url_file, pattern)
        except KeyboardInterrupt:
            print("stopped", flush=True)
            return 0
        except OSError as exc:
            print("ssh failed to start: %s" % exc, flush=True)
        lived = time.time() - started
        print("tunnel dropped after %.0fs, reconnecting" % lived, flush=True)
        time.sleep(args.retry_seconds)


if __name__ == "__main__":
    sys.exit(main())
