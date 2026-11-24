# -*- coding: utf-8 -*-
"""任务的文件区与会话状态。

两条接口回答的是同一批会话的两个不同问题：**文件区**说「它跑过的地方留下了
什么」，**会话状态**说「现在还有没有在动」。它们的共同前提是「任务没有自己的
目录」——文件就是它名下那几条对话的工作区。这一组用例钉的就是这条前提：
路径的第一段必须是会话号、会话号必须属于这条任务、越界一律拒绝。
"""
import os
import unittest
import urllib.error
import urllib.request

from tests import support
from pentestd import workspace


def _task(store, target="10.10.10.110"):
    return store.create_engagement(target, "拿下控制台", "本人授权")["id"]


class _FilesCase(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("task-files.db")
        # 工作区根指到临时目录：默认那条路径会落到仓库旁边的 workspace/，
        # 测试不该往真实部署的目录里写东西。
        self.ws = os.path.join(self.directory.name, "ws")
        self._old = os.environ.get(workspace.ENV_DIR)
        os.environ[workspace.ENV_DIR] = self.ws
        self.httpd, self.client = support.start(self.store)

    def tearDown(self):
        if self._old is None:
            os.environ.pop(workspace.ENV_DIR, None)
        else:
            os.environ[workspace.ENV_DIR] = self._old
        support.stop(self.httpd, self.store, self.directory)

    def yes(self, result, expect=200):
        status, payload = result
        self.assertEqual(expect, status, payload)
        return payload

    def session(self, task_id, title="第一轮", user_id=""):
        return self.store.create_chat_session(title, user_id, engagement_id=task_id)["id"]

    def write(self, session_id, name, data=b"probe ok\n"):
        base = os.path.join(self.ws, "sessions", workspace.safe_id(session_id))
        os.makedirs(base, exist_ok=True)
        full = os.path.join(base, name)
        with open(full, "wb") as handle:
            handle.write(data)
        return full

    def raw(self, path):
        """取原始字节：文件下载回的是文件本身，不是 JSON。"""
        request = urllib.request.Request(self.client.base + path)
        try:
            with self.client.opener.open(request, timeout=30) as response:
                return response.status, response.read(), response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers.get("Content-Type", "")


class TaskFilesListTests(_FilesCase):
    def test_a_task_with_no_sessions_lists_nothing_and_creates_nothing(self):
        task_id = _task(self.store)
        payload = self.yes(self.client.get("/api/v1/tasks/%s/files" % task_id))
        self.assertEqual(task_id, payload["taskId"])
        self.assertEqual([], payload["sessions"])
        self.assertEqual(0, payload["sessionsTotal"])
        self.assertEqual({"files": 0, "dirs": 0, "bytes": 0}, payload["totals"])
        # **只读**：打开这一页不该在盘上凭空建目录。
        self.assertFalse(os.path.isdir(os.path.join(self.ws, "sessions")))

    def test_each_session_is_its_own_root_and_paths_carry_the_session_id(self):
        task_id = _task(self.store)
        first = self.session(task_id, "第一轮")
        second = self.session(task_id, "第二轮")
        self.write(first, "probe.py", b"print(1)\n")
        self.write(second, "loot.txt", b"admin:admin\n")
        payload = self.yes(self.client.get("/api/v1/tasks/%s/files" % task_id))
        self.assertEqual(2, payload["sessionCount"])
        by_id = {row["sessionId"]: row for row in payload["sessions"]}
        self.assertEqual(["probe.py"],
                         [entry["name"] for entry in by_id[first]["entries"]])
        self.assertEqual(["loot.txt"],
                         [entry["name"] for entry in by_id[second]["entries"]])
        # 路径前缀补成会话号：界面拿到一条就能直接拿去下载，不必自己拼。
        self.assertEqual(first + "/probe.py", by_id[first]["entries"][0]["path"])
        # 两条会话各一个文件，合计按**目录里真实盘上的字节**算，不按条目数估。
        self.assertEqual(2, payload["totals"]["files"])
        self.assertEqual(len(b"print(1)\n") + len(b"admin:admin\n"),
                         payload["totals"]["bytes"])

    def test_a_session_without_a_workspace_reports_exists_false(self):
        task_id = _task(self.store)
        session_id = self.session(task_id)
        payload = self.yes(self.client.get("/api/v1/tasks/%s/files" % task_id))
        row = payload["sessions"][0]
        self.assertEqual(session_id, row["sessionId"])
        self.assertFalse(row["exists"])
        self.assertEqual([], row["entries"])

    def test_session_filter_narrows_to_one_root(self):
        task_id = _task(self.store)
        first = self.session(task_id, "第一轮")
        self.session(task_id, "第二轮")
        self.write(first, "probe.py")
        payload = self.yes(self.client.get(
            "/api/v1/tasks/%s/files?sessionId=%s" % (task_id, first)))
        self.assertEqual(1, payload["sessionCount"])
        self.assertEqual(2, payload["sessionsTotal"])
        self.assertTrue(payload["sessionsTruncated"])

    def test_unknown_task_is_404(self):
        status, payload = self.client.get("/api/v1/tasks/nope/files")
        self.assertEqual(404, status, payload)


class TaskFileDownloadTests(_FilesCase):
    def test_download_returns_the_bytes_and_the_content_type(self):
        task_id = _task(self.store)
        session_id = self.session(task_id)
        self.write(session_id, "loot.txt", b"admin:admin\n")
        path = "%s/files/download?path=%s" % (task_id, session_id + "/loot.txt")
        status, body, content_type = self.raw("/api/v1/tasks/" + path)
        self.assertEqual(200, status)
        self.assertEqual(b"admin:admin\n", body)
        self.assertIn("text/plain", content_type)

    def test_a_path_without_a_session_segment_is_400(self):
        task_id = _task(self.store)
        status, payload = self.client.get(
            "/api/v1/tasks/%s/files/download?path=" % task_id)
        self.assertEqual(400, status, payload)

    def test_a_session_of_another_task_is_404_not_forbidden_by_luck(self):
        mine = _task(self.store, "10.10.10.110")
        other = _task(self.store, "10.10.10.111")
        foreign = self.session(other)
        self.write(foreign, "secret.txt", b"not yours\n")
        status, payload = self.client.get(
            "/api/v1/tasks/%s/files/download?path=%s" % (mine, foreign + "/secret.txt"))
        # 归属判定在路径校验之前：这个会话号压根不属于这条任务。
        self.assertEqual(404, status, payload)

    def test_traversal_is_refused(self):
        task_id = _task(self.store)
        session_id = self.session(task_id)
        self.write(session_id, "probe.py")
        status, payload = self.client.get(
            "/api/v1/tasks/%s/files/download?path=%s"
            % (task_id, session_id + "/../../../../data/app.db"))
        self.assertEqual(403, status, payload)

    def test_a_missing_file_is_404(self):
        task_id = _task(self.store)
        session_id = self.session(task_id)
        status, payload = self.client.get(
            "/api/v1/tasks/%s/files/download?path=%s" % (task_id, session_id + "/nope.txt"))
        self.assertEqual(404, status, payload)


class TaskFileArchiveTests(_FilesCase):
    def test_the_zip_carries_every_session_with_the_session_id_as_prefix(self):
        task_id = _task(self.store)
        first = self.session(task_id, "第一轮")
        second = self.session(task_id, "第二轮")
        self.write(first, "probe.py", b"print(1)\n")
        self.write(second, "loot.txt", b"admin:admin\n")
        status, body, content_type = self.raw(
            "/api/v1/tasks/%s/files/archive" % task_id)
        self.assertEqual(200, status)
        self.assertIn("application/zip", content_type)
        import io as _io
        import zipfile
        with zipfile.ZipFile(_io.BytesIO(body)) as bundle:
            self.assertEqual(sorted([first + "/probe.py", second + "/loot.txt"]),
                             sorted(bundle.namelist()))

    def test_a_session_prefix_narrows_the_archive_to_that_session(self):
        task_id = _task(self.store)
        first = self.session(task_id, "第一轮")
        second = self.session(task_id, "第二轮")
        self.write(first, "probe.py")
        self.write(second, "loot.txt")
        status, body, _ctype = self.raw(
            "/api/v1/tasks/%s/files/archive?path=%s" % (task_id, first))
        self.assertEqual(200, status)
        import io as _io
        import zipfile
        with zipfile.ZipFile(_io.BytesIO(body)) as bundle:
            self.assertEqual([first + "/probe.py"], bundle.namelist())

    def test_a_symlink_never_lands_in_the_package(self):
        task_id = _task(self.store)
        session_id = self.session(task_id)
        base = os.path.join(self.ws, "sessions", workspace.safe_id(session_id))
        self.write(session_id, "probe.py")
        link = os.path.join(base, "app.db")
        try:
            os.symlink(os.path.join(self.directory.name, "outside.db"), link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("这个平台建不了符号链接")
        status, body, _ctype = self.raw(
            "/api/v1/tasks/%s/files/archive" % task_id)
        self.assertEqual(200, status)
        import io as _io
        import zipfile
        with zipfile.ZipFile(_io.BytesIO(body)) as bundle:
            self.assertNotIn(session_id + "/app.db", bundle.namelist())
            self.assertIn(session_id + "/probe.py", bundle.namelist())

    def test_traversal_is_refused(self):
        task_id = _task(self.store)
        session_id = self.session(task_id)
        self.write(session_id, "probe.py")
        status, payload = self.client.get(
            "/api/v1/tasks/%s/files/archive?path=%s"
            % (task_id, session_id + "/../../../../data"))
        # 越界不是「什么都没有」，是「这个目录不存在」——它不该被当成空包发出去。
        self.assertEqual(404, status, payload)

    def test_a_task_with_no_files_is_404_not_an_empty_zip(self):
        task_id = _task(self.store)
        status, payload = self.client.get(
            "/api/v1/tasks/%s/files/archive" % task_id)
        self.assertEqual(404, status, payload)


class TaskFileReadTests(_FilesCase):
    def test_a_text_file_comes_back_as_text_with_its_size(self):
        task_id = _task(self.store)
        session_id = self.session(task_id)
        self.write(session_id, "probe.txt", b"probe ok\n")
        payload = self.yes(self.client.get(
            "/api/v1/tasks/%s/files/read?path=%s/probe.txt" % (task_id, session_id)))
        self.assertEqual("probe ok\n", payload["text"])
        self.assertFalse(payload["binary"])
        self.assertFalse(payload["truncated"])
        self.assertEqual(len(b"probe ok\n"), payload["size"])

    def test_binary_is_reported_not_decoded(self):
        task_id = _task(self.store)
        session_id = self.session(task_id)
        self.write(session_id, "dump.bin", b"\x7fELF\x00\x01\x02payload")
        payload = self.yes(self.client.get(
            "/api/v1/tasks/%s/files/read?path=%s/dump.bin" % (task_id, session_id)))
        # 判二进制只看前 8 KiB 里有没有 NUL，不看扩展名：这里连名字都是 .bin，
        # 换成 .txt 也一样得判成二进制。
        self.assertTrue(payload["binary"])
        self.assertEqual("", payload["text"])

    def test_a_path_without_a_session_segment_is_400(self):
        task_id = _task(self.store)
        status, payload = self.client.get(
            "/api/v1/tasks/%s/files/read?path=" % task_id)
        self.assertEqual(400, status, payload)

    def test_a_bare_file_name_is_404_because_the_first_segment_is_a_session(self):
        task_id = _task(self.store)
        session_id = self.session(task_id)
        self.write(session_id, "probe.txt")
        # 只给文件名：第一段被当成会话号，而它不属于这条任务。归属判定排在路径
        # 校验之前，所以这里回的是 404 而不是「文件不存在」那种 404 之外的东西。
        status, payload = self.client.get(
            "/api/v1/tasks/%s/files/read?path=probe.txt" % task_id)
        self.assertEqual(404, status, payload)

    def test_traversal_is_refused(self):
        task_id = _task(self.store)
        session_id = self.session(task_id)
        self.write(session_id, "probe.txt")
        status, payload = self.client.get(
            "/api/v1/tasks/%s/files/read?path=%s/../../../../etc/passwd"
            % (task_id, session_id))
        self.assertEqual(403, status, payload)

    def test_a_missing_file_is_404(self):
        task_id = _task(self.store)
        session_id = self.session(task_id)
        self.write(session_id, "probe.txt")
        status, payload = self.client.get(
            "/api/v1/tasks/%s/files/read?path=%s/nope.txt" % (task_id, session_id))
        self.assertEqual(404, status, payload)


class TaskChatStatusTests(_FilesCase):
    def test_a_task_with_no_sessions_is_idle(self):
        task_id = _task(self.store)
        payload = self.yes(self.client.get("/api/v1/tasks/%s/chat/status" % task_id))
        self.assertEqual(0, payload["count"])
        self.assertEqual(0, payload["active"])
        self.assertIn("heartbeatSeconds", payload)
        self.assertIn("serverTime", payload)

    def test_every_session_reports_whether_it_runs_right_now(self):
        task_id = _task(self.store)
        self.session(task_id, "第一轮")
        payload = self.yes(self.client.get("/api/v1/tasks/%s/chat/status" % task_id))
        self.assertEqual(1, payload["count"])
        row = payload["sessions"][0]
        self.assertFalse(row["running"])
        self.assertFalse(row["cancelling"])
        self.assertEqual(0, row["inbox"])

    def test_unknown_task_is_404(self):
        status, payload = self.client.get("/api/v1/tasks/nope/chat/status")
        self.assertEqual(404, status, payload)

    def test_an_operator_only_sees_the_sessions_they_own(self):
        from pentestd import auth
        task_id = _task(self.store)
        admin_row = self.store.conn.execute(
            "SELECT id FROM users WHERE username='admin'").fetchone()
        # 归属得是真的「别人的」：`userId` 为空的会话算公共，谁都看得见，
        # 所以这里必须用管理员自己的 user id 建，才测得到 hidden 那条分支。
        admin = self.store.create_chat_session("管理员的", admin_row["id"], engagement_id=task_id)
        payload = self.yes(self.client.get("/api/v1/tasks/%s/chat/status" % task_id))
        self.assertEqual("all", payload["scope"])
        self.assertEqual(1, payload["count"])

        self.store.create_user("operator", auth.hash_password("Operator-Pass1!"),
                              role="operator")
        operator_row = self.store.conn.execute(
            "SELECT id FROM users WHERE username='operator'").fetchone()
        self.session(task_id, "操作员的", operator_row["id"])
        base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        client = support.Client(base)
        self.assertEqual(200, client.login("operator", "Operator-Pass1!")[0])
        seen = self.yes(client.get("/api/v1/tasks/%s/chat/status" % task_id))
        self.assertEqual("own", seen["scope"])
        # 只剩他自己那条：管理员那条带的是管理员的 user id，归进 hidden。
        # （userId 为空的会话算公共，谁都看得见，那种情况不在这条用例里。）
        self.assertEqual(1, seen["count"])
        self.assertEqual(1, seen["hidden"])
        self.assertNotEqual(admin["id"], seen["sessions"][0]["sessionId"])

class TaskChatStopTests(_FilesCase):
    """在任务详情里停掉它名下某一条对话。

    真正的"停下来"由 `tests/test_turnpump.py` 用真模型流钉着（那一套要点火、要
    等收尾，不该在文件区这组里再搭一次）。这里钉的是三件事：**这一层自己的判据**
    ——归属、缺参、别人家的对话——以及"没在跑时说人话"。
    """

    def test_stopping_an_idle_session_says_it_was_not_running(self):
        task_id = _task(self.store)
        session_id = self.session(task_id, "跑完了的")
        payload = self.yes(self.client.post(
            "/api/v1/tasks/%s/chat/stop" % task_id, {"sessionId": session_id}))
        self.assertEqual(task_id, payload["taskId"])
        self.assertEqual(session_id, payload["sessionId"])
        self.assertFalse(payload["stopping"])
        self.assertFalse(payload["active"])
        # 这一刻没在跑时说「会在当前这一步之后结束」是在描述一件不存在的事。
        self.assertIn("没有在跑", payload["note"])

    def test_a_missing_session_id_is_400(self):
        task_id = _task(self.store)
        status, payload = self.client.post(
            "/api/v1/tasks/%s/chat/stop" % task_id, {})
        self.assertEqual(400, status, payload)

    def test_a_session_of_another_task_is_404(self):
        mine = _task(self.store, "10.10.10.110")
        other = _task(self.store, "10.10.10.111")
        foreign = self.session(other, "别处的")
        # 归属判定在停止之前：不判的话，手里有一条任务的管理权限就能停别人的对话。
        status, payload = self.client.post(
            "/api/v1/tasks/%s/chat/stop" % mine, {"sessionId": foreign})
        self.assertEqual(404, status, payload)
        self.assertIn("这个对话", payload["error"])

    def test_unknown_task_is_404(self):
        status, payload = self.client.post(
            "/api/v1/tasks/nope/chat/stop", {"sessionId": "chat-1"})
        self.assertEqual(404, status, payload)

    def test_an_operator_cannot_stop_someone_elses_session(self):
        from pentestd import auth
        task_id = _task(self.store)
        admin_row = self.store.conn.execute(
            "SELECT id FROM users WHERE username='admin'").fetchone()
        theirs = self.store.create_chat_session("管理员的", admin_row["id"],
                                               engagement_id=task_id)["id"]
        self.store.create_user("operator", auth.hash_password("Operator-Pass1!"),
                              role="operator")
        base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        client = support.Client(base)
        self.assertEqual(200, client.login("operator", "Operator-Pass1!")[0])
        status, payload = client.post(
            "/api/v1/tasks/%s/chat/stop" % task_id, {"sessionId": theirs})
        # 归属过了（是这条任务的），但那条对话不归他——闸在 `_require_chat_access`。
        self.assertEqual(403, status, payload)
