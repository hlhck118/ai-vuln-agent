# -*- coding: utf-8 -*-
"""演示目标：Web 服务片段（含缺陷），覆盖四类可动态验证的缺陷。

本文件仅用于验证自动验证能力，请勿在生产环境使用。

缺陷清单（供扫描器对照）：
  CWE-79   反射型 XSS         render_comment 把昵称直接拼进 HTML
  CWE-95   代码注入           calc 把表达式直接交给 eval
  CWE-327  弱哈希             sign 使用 MD5 且无盐
  CWE-918  服务端请求伪造     fetch 按外部传入的 URL 直接请求
"""
import hashlib
import sys
import urllib.request


def render_comment(name):
    """把用户昵称直接拼进 HTML 输出（CWE-79）。"""
    return "<div class='comment'>Hello, " + name + "!</div>"


def calc(expr):
    """计算器：把表达式直接交给 eval 求值（CWE-95）。"""
    return eval(expr)


def sign(password):
    """口令签名：MD5 且无盐（CWE-327）。"""
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def fetch(url):
    """抓取远端内容：URL 完全由外部决定（CWE-918）。"""
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read().decode("utf-8", "ignore")


def handle(argv):
    """命令行入口，按子命令驱动上面的业务函数。"""
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
