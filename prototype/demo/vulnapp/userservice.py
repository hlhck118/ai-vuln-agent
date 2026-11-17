# -*- coding: utf-8 -*-
"""演示目标程序（含已知缺陷）。

本文件仅用于验证自动化漏洞发掘闭环，故意包含若干典型缺陷，
请勿在生产环境使用，也不要复制其中的写法。

缺陷清单（供扫描器对照）：
  CWE-89   SQL 注入        find_user 中的字符串拼接 SQL
  CWE-78   命令注入        render_banner 中 shell=True 拼接
  CWE-22   路径穿越        read_doc 中直接拼接用户传入的文件名
  CWE-502  不安全反序列化  load_session 中 pickle.loads
  CWE-327  弱哈希          hash_password 使用 MD5
  CWE-798  硬编码凭据      API_TOKEN 明文常量
"""
import base64
import hashlib
import os
import pickle
import sqlite3
import subprocess
import sys

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "files")
API_TOKEN = "prod-token-8f3c9a1b-2d7e"          # CWE-798 硬编码凭据
DB_SEED = [("alice", "user"), ("bob", "admin")]


def get_conn():
    """创建一个内存数据库并写入种子数据。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (name TEXT, role TEXT)")
    conn.executemany("INSERT INTO users VALUES (?, ?)", DB_SEED)
    return conn


def find_user(conn, username):
    """按用户名查询：SQL 由字符串拼接构造（CWE-89）。"""
    cur = conn.cursor()
    sql = "SELECT name, role FROM users WHERE name = '%s'" % username
    cur.execute(sql)
    return cur.fetchall()


def render_banner(text):
    """把横幅文本交给 shell 处理后返回输出（CWE-78）。"""
    completed = subprocess.run("echo " + text, shell=True,
                               capture_output=True, text=True)
    return completed.stdout


def read_doc(name):
    """读取文档目录下的文件（CWE-22）。"""
    path = os.path.join(BASE_DIR, name)
    with open(path, "r", encoding="utf-8", errors="ignore") as handle:
        return handle.read()


def load_session(blob):
    """还原会话对象（CWE-502）。"""
    raw = base64.b64decode(blob)
    return pickle.loads(raw)


def hash_password(password):
    """口令散列（CWE-327）。"""
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def dispatch(argv):
    """命令行入口，按子命令调用上面的业务函数。"""
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
