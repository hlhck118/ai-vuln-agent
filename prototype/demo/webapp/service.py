# -*- coding: utf-8 -*-
"""业务层（演示目标：跨文件调用 + 同名方法消歧）。"""
import dao
from dao import OrderDao, UserDao


class ProfileService:
    """通过 self.attr 持有不同 DAO，考察类型推断能力。"""

    def __init__(self, conn):
        self.conn = conn
        self.user_dao = UserDao()
        self.order_dao = OrderDao()

    def persist(self, row):
        """应解析到 dao::UserDao.save，而不是 OrderDao.save。"""
        return self.user_dao.save(row)

    def archive(self, row):
        """应解析到 dao::OrderDao.save。"""
        return self.order_dao.save(row)


def load_profile(conn, name):
    """跨文件调用数据访问层。"""
    return dao.find_user(conn, name)
