# -*- coding: utf-8 -*-
"""数据访问层的**修复版本**：参数化查询，其余结构与缺陷版一致。"""
import sqlite3

DB_SEED = [("alice", "user"), ("bob", "admin")]


def get_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (name TEXT, role TEXT)")
    conn.executemany("INSERT INTO users VALUES (?, ?)", DB_SEED)
    return conn


def find_user(conn, username):
    """参数化查询：用户输入不再参与 SQL 构造。"""
    cur = conn.cursor()
    cur.execute("SELECT name, role FROM users WHERE name = ?", (username,))
    return cur.fetchall()


class UserDao:
    def save(self, row):
        return "user:%s" % row


class OrderDao:
    def save(self, row):
        return "order:%s" % row
