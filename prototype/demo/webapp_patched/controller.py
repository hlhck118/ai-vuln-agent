# -*- coding: utf-8 -*-
"""接入层（演示目标：外部入口，跨文件调用链的起点）。"""
import sys

import dao
import service


def handle(argv):
    """命令行入口：从外部接收参数并驱动整条业务链。"""
    if not argv:
        return 1
    conn = dao.get_conn()
    profile_service = service.ProfileService(conn)
    action = argv[0]
    value = argv[1] if len(argv) > 1 else ""
    if action == "profile":
        print(service.load_profile(conn, value))
    elif action == "persist":
        print(profile_service.persist(value))
    elif action == "archive":
        print(profile_service.archive(value))
    else:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(handle(sys.argv[1:]))
