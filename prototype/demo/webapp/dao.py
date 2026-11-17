# -*- coding: utf-8 -*-
"""数据访问层（演示目标：跨文件调用链末端，含 SQL 注入缺陷）。

该文件与 service.py、controller.py 共同构成一条跨三层的调用链：
    controller.handle -> service.load_profile -> dao.find_user -> cur.execute
用于验证跨文件可达性分析与同名方法消歧能力。
"""
import sqlite3

DB_SEED = [("alice", "user"), ("bob", "admin")]


def get_conn():
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


class UserDao:
    """用户数据访问对象。"""

    def save(self, row):
        return "user:%s" % row


class OrderDao:
    """订单数据访问对象：与 UserDao 同名方法，用于验证解析是否串味。"""

    def save(self, row):
        return "order:%s" % row
