# -*- coding: utf-8 -*-
"""演示目标程序的**修复版本**，函数名与签名与缺陷版保持一致。

用途：与缺陷版做"差分验证"——同一个验证用例分别打在缺陷版与修复版上，
只有缺陷版复现出效果、修复版不能时，才判定漏洞成立。

修复要点：
  CWE-89   改用参数化查询（占位符绑定）
  CWE-78   关闭 shell，改用参数数组，并做字符白名单
  CWE-22   只取 basename 并限制在 BASE_DIR 之内
  CWE-502  改用 JSON 反序列化
  CWE-327  改用 PBKDF2-HMAC-SHA256 加盐派生
  CWE-798  凭据改为从环境变量读取
"""
import base64
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "files")
API_TOKEN = os.environ.get("VULNAPP_TOKEN", "")          # 不再硬编码
DB_SEED = [("alice", "user"), ("bob", "admin")]
_SAFE_BANNER = re.compile(r"^[A-Za-z0-9 _.-]*$")


def get_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (name TEXT, role TEXT)")
    conn.executemany("INSERT INTO users VALUES (?, ?)", DB_SEED)
    return conn


def find_user(conn, username):
    """参数化查询，用户输入不再参与 SQL 构造。"""
    cur = conn.cursor()
    cur.execute("SELECT name, role FROM users WHERE name = ?", (username,))
    return cur.fetchall()


def render_banner(text):
    """白名单校验 + 关闭 shell + 参数数组传参。"""
    if not _SAFE_BANNER.match(text or ""):
        raise ValueError("banner 文本包含非法字符")
    completed = subprocess.run(["echo", text], shell=False,
                               capture_output=True, text=True)
    return completed.stdout


def read_doc(name):
    """只允许读取 BASE_DIR 下的单层文件名。"""
    safe = os.path.basename(name or "")
    path = os.path.join(BASE_DIR, safe)
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(BASE_DIR) + os.sep):
        raise ValueError("路径越界")
    with open(real, "r", encoding="utf-8", errors="ignore") as handle:
        return handle.read()


def load_session(blob):
    """改用 JSON，杜绝任意对象构造。"""
    raw = base64.b64decode(blob)
    return json.loads(raw.decode("utf-8"))


def hash_password(password):
    """PBKDF2-HMAC-SHA256 加盐派生。"""
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 b"vulnapp-demo-salt", 100000)
    return digest.hex()


def dispatch(argv):
    if not argv:
        return 1
    action = argv[0]
    if action == "find":
        conn = get_conn()
        print(find_user(conn, argv[1] if len(argv) > 1 else ""))
    elif action == "banner":
        print(render_banner(argv[1] if len(argv) > 1 else ""))
    elif action == "read":
        print(read_doc(argv[1] if len(argv) > 1 else "readme.txt"))
    elif action == "session":
        print(load_session(argv[1] if len(argv) > 1 else ""))
    elif action == "hash":
        print(hash_password(argv[1] if len(argv) > 1 else ""))
    else:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(dispatch(sys.argv[1:]))
