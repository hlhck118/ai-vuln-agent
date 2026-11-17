# -*- coding: utf-8 -*-
"""演示目标 portal 的**修复版本**，函数名与签名保持一致。

修复要点：
  CWE-79   输出统一经 html.escape 转义
  CWE-95   取消 eval，改用显式允许列表映射
  CWE-327  改用 PBKDF2-HMAC-SHA256 加盐派生
  CWE-918  限制协议为 http/https，并校验主机白名单
"""
import hashlib
import html
import sys
import urllib.parse
import urllib.request

ALLOWED_SCHEMES = ("http", "https")
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
_SAFE_EXPR = {"1+1": 2, "2*3": 6}


def render_comment(name):
    return "<div class='comment'>Hello, " + html.escape(name or "") + "!</div>"


def calc(expr):
    if expr not in _SAFE_EXPR:
        raise ValueError("表达式不在允许列表内")
    return _SAFE_EXPR[expr]


def sign(password):
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 b"portal-demo-salt", 100000)
    return digest.hex()


def fetch(url):
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ValueError("仅允许 http/https 协议")
    if parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("目标地址不在白名单内")
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read().decode("utf-8", "ignore")


def handle(argv):
    if not argv:
        return 1
    action = argv[0]
    value = argv[1] if len(argv) > 1 else ""
    if action == "comment":
        print(render_comment(value))
    elif action == "calc":
        print(calc(value))
    elif action == "sign":
        print(sign(value))
    elif action == "fetch":
        print(fetch(value))
    else:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(handle(sys.argv[1:]))
