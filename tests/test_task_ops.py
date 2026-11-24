# -*- coding: utf-8 -*-
"""任务运营：分类、模板、建任务、批量动作、授权范围、覆盖度、情报、模型、意图、拦截。

这一组用例钉的是「任务页上每一个动作在服务端到底落在哪一行」。它们分两类：
一类是**写入语义**（全量替换的范围、删除分类只解绑不删任务、情报的校验顺序），
一类是**口径合并**（任务级拦截规则 = 全局 + 本任务，排序只有一处）。
前者错了，界面会显示一个和实际不符的结果；后者错了，界面说没拦、实际却拦了。
"""
import types
import unittest

from tests import support
from pentestd import auth, intercept


def _task(store, target="10.10.10.110"):
    return store.create_engagement(target, "拿下控制台", "本人授权")["id"]


def _profile(store, name="任务模型"):
    return store.save_profile("", name, "deepseek", "http://127.0.0.1:1/deepseek",
                              "deepseek-chat", "none", "", None, 0.0, 512, 30, "")


class _TaskOpsCase(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("task-ops.db")
        self.httpd, self.client = support.start(self.store)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def yes(self, result, expect=200):
        status, payload = result
        self.assertEqual(expect, status, payload)
        return payload

    def operator(self):
        """建一个操作员账号并登录。写操作要管理员，读只要登录，两档都得测。"""
        self.store.create_user("operator", auth.hash_password("Operator-Pass1!"),
                              role="operator")
        base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        client = support.Client(base)
        status, payload = client.login("operator", "Operator-Pass1!")
        self.assertEqual(200, status, payload)
        return client


class TaskCategoryTests(_TaskOpsCase):
    def test_create_read_delete_keeps_the_tasks(self):
        eid = _task(self.store)
        made = self.yes(self.client.post("/api/v1/task-categories",
                                         {"name": "红队项目", "color": "#ff0000",
                                          "sort": 2}), 201)
        self.assertTrue(made["id"].startswith("tc-"))
        self.assertEqual("红队项目", made["name"])
        self.assertEqual(2, made["sort"])

        listed = self.yes(self.client.get("/api/v1/task-categories"))
        self.assertEqual(1, listed["total"])
        self.assertEqual(1, listed["uncategorized"]["all"])
        self.assertEqual(0, listed["categories"][0]["counts"]["all"])

        self.yes(self.client.post("/api/v1/tasks/category/batch",
                                  {"taskIds": [eid], "categoryId": made["id"]}))
        listed = self.yes(self.client.get("/api/v1/task-categories"))
        self.assertEqual(1, listed["categories"][0]["counts"]["all"])
        self.assertEqual(1, listed["categories"][0]["counts"]["active"])
        self.assertEqual(0, listed["uncategorized"]["all"])

        gone = self.yes(self.client.post(
            "/api/v1/task-categories/%s/delete" % made["id"]))
        self.assertTrue(gone["deleted"])
        self.assertEqual(1, gone["unassigned"])

        # 删分组不等于删组里的东西：任务还在，只是回到未分类。
        status, task = self.client.get("/api/v1/tasks/%s" % eid)
        self.assertEqual(200, status, task)
        self.assertEqual("", task["categoryId"])
        self.assertEqual(0, self.yes(self.client.get("/api/v1/task-categories"))[
            "uncategorized"]["archived"])

    def test_names_are_unique_and_never_blank(self):
        self.yes(self.client.post("/api/v1/task-categories", {"name": "红队"}), 201)
        status, payload = self.client.post("/api/v1/task-categories",
                                           {"name": "红队"})
        self.assertEqual(400, status, payload)
        self.assertIn("同名分类", payload["error"])
        # 大小写不同也算同名：分类是给人看的分组，大小写两行等于两个分组。
        status, payload = self.client.post("/api/v1/task-categories",
                                           {"name": " 红队 "})
        self.assertEqual(400, status, payload)
        status, payload = self.client.post("/api/v1/task-categories", {"name": " "})
        self.assertEqual(400, status, payload)
        self.assertIn("分类名不能为空", payload["error"])

    def test_update_by_id_answers_200_not_201(self):
        made = self.yes(self.client.post("/api/v1/task-categories",
                                         {"name": "红队"}), 201)
        changed = self.yes(self.client.post("/api/v1/task-categories",
                                            {"id": made["id"], "name": "蓝队",
                                             "remark": "改过"}), 200)
        self.assertEqual("蓝队", changed["name"])
        self.assertEqual("改过", changed["remark"])
        self.assertEqual(1, self.yes(self.client.get(
            "/api/v1/task-categories"))["total"])


class TaskTemplateTests(_TaskOpsCase):
    def test_template_carries_configuration_not_evidence(self):
        made = self.yes(self.client.post("/api/v1/task-templates", {
            "name": "标准外网", "target": "10.10.10.111",
            "objective": "拿权限", "authorization": "授权书 47 号",
            "goals": ["拿到后台", "拿到数据库"],
            "scope": [{"kind": "allow", "text": "10.10.10.0/24"},
                      {"kind": "deny", "text": "10.10.10.5"}],
            "workerCount": 3, "coverageEnabled": True}), 201)
        self.assertTrue(made["id"].startswith("tt-"))
        self.assertEqual(2, len(made["goals"]))
        self.assertEqual("deny", made["scope"][1]["kind"])
        self.assertEqual(3, made["workerCount"])
        self.assertTrue(made["coverageEnabled"])

        listed = self.yes(self.client.get("/api/v1/task-templates"))
        self.assertEqual(1, listed["total"])
        self.assertEqual("标准外网", listed["templates"][0]["name"])

        gone = self.yes(self.client.post(
            "/api/v1/task-templates/%s/delete" % made["id"]))
        self.assertTrue(gone["deleted"])
        self.assertEqual(0, self.yes(self.client.get(
            "/api/v1/task-templates"))["total"])

    def test_scope_entries_are_checked_not_coerced(self):
        status, payload = self.client.post("/api/v1/task-templates", {
            "name": "歪的", "scope": [{"kind": "maybe", "text": "x"}]})
        self.assertEqual(400, status, payload)
        self.assertIn("allow", payload["error"])
        status, payload = self.client.post("/api/v1/task-templates",
                                           {"name": " "})
        self.assertEqual(400, status, payload)
        self.assertIn("模板名不能为空", payload["error"])


class TaskCreateTests(_TaskOpsCase):
    def test_create_from_template_lands_one_row_with_everything(self):
        category = self.yes(self.client.post("/api/v1/task-categories",
                                             {"name": "红队"}), 201)
        template = self.yes(self.client.post("/api/v1/task-templates", {
            "name": "标准外网", "target": "10.10.10.111",
            "goals": ["第二个目标"],
            "scope": [{"kind": "allow", "text": "10.10.10.0/24"}]}), 201)
        made = self.yes(self.client.post("/api/v1/tasks/create", {
            "templateId": template["id"], "categoryId": category["id"],
            "name": "六月的外网"}), 201)
        self.assertEqual("六月的外网", made["name"])
        self.assertEqual("10.10.10.111", made["target"])
        self.assertEqual("红队", made["categoryName"])
        self.assertEqual(template["id"], made["templateId"])
        self.assertEqual("idle", made["status"])

        # 建档写下 goal-1，模板里其余的按 seq 追加：目标列表是「要做什么」的全集。
        self.assertEqual(2, made["counts"]["goal"])
        allowed = self.store.list_constraints(made["id"], "allow")
        self.assertEqual(["10.10.10.0/24"], [row["text"] for row in allowed])

    def test_unknown_template_and_blank_target_are_rejected(self):
        status, payload = self.client.post("/api/v1/tasks/create",
                                           {"templateId": "tt-nope"})
        self.assertEqual(404, status, payload)
        self.assertIn("任务模板不存在", payload["error"])
        status, payload = self.client.post("/api/v1/tasks/create",
                                           {"target": ""})
        self.assertEqual(400, status, payload)
        self.assertIn("不能为空", payload["error"])

    def test_the_new_task_shows_up_in_the_list_right_away(self):
        self.yes(self.client.post("/api/v1/tasks/create", {"target": "10.10.10.112"}),
                201)
        listed = self.yes(self.client.get("/api/v1/tasks"))
        self.assertEqual(1, listed["total"])
        self.assertEqual("10.10.10.112", listed["tasks"][0]["target"])
        self.assertEqual(1, listed["counts"]["idle"])


class TaskControlTests(_TaskOpsCase):
    def test_batch_actions_report_which_ids_missed(self):
        first = _task(self.store, "10.10.10.120")
        second = _task(self.store, "10.10.10.121")
        result = self.yes(self.client.post("/api/v1/tasks/control/batch",
                                          {"taskIds": [first, second, "E-nope"],
                                           "action": "pause"}))
        self.assertEqual("pause", result["action"])
        self.assertEqual(2, result["updated"])
        self.assertEqual(["E-nope"], result["missing"])
        self.assertTrue(self.store.get_task(first)["paused"])
        self.assertEqual(2, result["counts"]["paused"])

        self.yes(self.client.post("/api/v1/tasks/%s/control" % first,
                                 {"action": "resume"}))
        self.assertFalse(self.store.get_task(first)["paused"])

    def test_stop_leaves_a_reason_behind(self):
        eid = _task(self.store)
        self.yes(self.client.post("/api/v1/tasks/%s/control" % eid,
                                 {"action": "stop"}))
        task = self.store.get_task(eid)
        self.assertTrue(task["paused"])
        self.assertEqual("手动停止", task["stopReason"])

    def test_pin_and_archive_move_the_task_out_of_the_default_view(self):
        eid = _task(self.store)
        self.yes(self.client.post("/api/v1/tasks/%s/control" % eid,
                                 {"action": "pin"}))
        self.assertTrue(self.store.get_task(eid)["pinned"])
        self.yes(self.client.post("/api/v1/tasks/%s/control" % eid,
                                 {"action": "archive"}))
        self.assertEqual(0, self.yes(self.client.get("/api/v1/tasks"))["total"])
        archived = self.yes(self.client.get("/api/v1/tasks?archived=1"))
        self.assertEqual(1, archived["total"])
        self.yes(self.client.post("/api/v1/tasks/%s/control" % eid,
                                 {"action": "unarchive"}))
        self.assertEqual(1, self.yes(self.client.get("/api/v1/tasks"))["total"])

    def test_bad_action_and_empty_batch_are_400(self):
        eid = _task(self.store)
        status, payload = self.client.post("/api/v1/tasks/control/batch",
                                           {"taskIds": [eid], "action": "explode"})
        self.assertEqual(400, status, payload)
        self.assertIn("动作只能是", payload["error"])
        status, payload = self.client.post("/api/v1/tasks/control/batch",
                                           {"taskIds": [], "action": "pause"})
        self.assertEqual(400, status, payload)
        self.assertIn("要指定任务", payload["error"])

    def test_category_batch_moves_tasks_and_names_the_unknown_category(self):
        first = _task(self.store, "10.10.10.130")
        second = _task(self.store, "10.10.10.131")
        category = self.yes(self.client.post("/api/v1/task-categories",
                                             {"name": "红队"}), 201)
        moved = self.yes(self.client.post("/api/v1/tasks/category/batch",
                                          {"taskIds": [first, "E-nope"],
                                           "categoryId": category["id"]}))
        self.assertEqual([first], moved["updated"])
        self.assertEqual(["E-nope"], moved["missing"])
        status, payload = self.client.post("/api/v1/tasks/category/batch",
                                           {"taskIds": [second],
                                            "categoryId": "tc-nope"})
        self.assertEqual(404, status, payload)
        self.assertIn("分类不存在", payload["error"])
        # 取消分类用空串：落回未分类，不是「不接受这个动作」。
        self.yes(self.client.post("/api/v1/tasks/category/batch",
                                  {"taskIds": [first], "categoryId": ""}))
        self.assertEqual("", self.store.get_task(first)["categoryId"])


class TaskArchiveSummaryTests(_TaskOpsCase):
    def test_archive_center_groups_by_category(self):
        category = self.yes(self.client.post("/api/v1/task-categories",
                                             {"name": "红队"}), 201)
        first = _task(self.store, "10.10.10.140")
        second = _task(self.store, "10.10.10.141")
        self.store.update_task(first, {"categoryId": category["id"]})
        self.yes(self.client.post("/api/v1/tasks/control/batch",
                                 {"taskIds": [first, second],
                                  "action": "archive"}))
        summary = self.yes(self.client.get("/api/v1/tasks/archive/summary"))
        self.assertEqual(2, summary["total"])
        names = [group["categoryName"] for group in summary["groups"]]
        self.assertIn("红队", names)
        self.assertIn("未分类", names)
        self.assertEqual(2, summary["counts"]["archived"])
        # 分组里的条目要能认出是哪一条：只有计数没有 id 的话，点进去只能靠猜。
        red = [group for group in summary["groups"]
               if group["categoryName"] == "红队"][0]
        self.assertEqual([first], [row["id"] for row in red["tasks"]])


class TaskScopeTests(_TaskOpsCase):
    def test_scope_is_replaced_whole_not_patched(self):
        eid = _task(self.store)
        first = self.yes(self.client.post("/api/v1/tasks/%s/scope" % eid, {
            "entries": [{"kind": "allow", "text": "10.10.10.0/24"},
                        {"kind": "deny", "text": "10.10.10.5"}]}))
        self.assertEqual(2, first["added"])
        self.assertEqual(0, first["removed"])

        listed = self.yes(self.client.get("/api/v1/tasks/%s/scope" % eid))
        self.assertEqual(1, listed["counts"]["allow"])
        self.assertEqual(1, listed["counts"]["deny"])
        self.assertEqual(["allow", "deny"], list(listed["kinds"]))

        second = self.yes(self.client.post("/api/v1/tasks/%s/scope" % eid, {
            "entries": [{"kind": "allow", "text": "10.10.10.0/24"},
                        {"kind": "allow", "text": "10.10.20.0/24"}]}))
        self.assertEqual(1, second["added"])
        self.assertEqual(1, second["removed"])
        self.assertEqual(2, len(second["entries"]))
        self.assertEqual(["allow", "allow"],
                         [row["kind"] for row in second["entries"]])

    def test_bad_scope_payload_is_400(self):
        eid = _task(self.store)
        status, payload = self.client.post("/api/v1/tasks/%s/scope" % eid,
                                           {"entries": "10.10.10.0/24"})
        self.assertEqual(400, status, payload)
        self.assertIn("entries 必须是数组", payload["error"])
        status, payload = self.client.post("/api/v1/tasks/%s/scope" % eid,
                                           {"entries": [{"kind": "maybe",
                                                         "text": "x"}]})
        self.assertEqual(400, status, payload)
        self.assertIn("kind 只能是", payload["error"])
        status, payload = self.client.get("/api/v1/tasks/E-nope/scope")
        self.assertEqual(404, status, payload)


class TaskCoverageTests(_TaskOpsCase):
    def test_only_conclusions_count_as_covered(self):
        eid = _task(self.store)
        covered = self.store.add_node(eid, "asset", {"type": "ip",
                                                     "value": "10.10.10.150"})
        idle = self.store.add_node(eid, "asset", {"type": "ip",
                                                  "value": "10.10.10.151"})
        intent = self.store.add_node(eid, "intent", {
            "title": "扫 150", "goalId": "goal-1",
            "assetIds": [covered["id"]]})
        self.store.update_node_state(eid, intent["id"], "done")

        data = self.yes(self.client.get("/api/v1/tasks/%s/coverage" % eid))
        self.assertEqual(2, data["summary"]["assets"])
        self.assertEqual(1, data["summary"]["tested"])
        self.assertEqual(1, data["summary"]["untested"])
        self.assertEqual(50, data["summary"]["percent"])
        rows = {row["assetId"]: row for row in data["assets"]}
        self.assertTrue(rows[covered["id"]]["tested"])
        self.assertEqual(1, rows[covered["id"]]["intents"])
        self.assertFalse(rows[idle["id"]]["tested"])
        self.assertFalse(data["enabled"])

        refs = self.yes(self.client.get("/api/v1/tasks/%s/asset-refs" % eid))
        self.assertEqual(2, refs["total"])
        by_id = {row["assetId"]: row for row in refs["assets"]}
        self.assertEqual(covered["id"], [row["assetId"] for row in refs["assets"]
                                        if row["tested"]][0])
        self.assertTrue(by_id[covered["id"]]["tested"])

    def test_a_finding_alone_marks_the_asset_covered(self):
        eid = _task(self.store)
        asset = self.store.add_node(eid, "asset", {"type": "app",
                                                   "value": "portal"})
        intent = self.store.add_node(eid, "intent", {"title": "打 portal",
                                                     "goalId": "goal-1"})
        self.store.add_node(eid, "finding", {
            "title": "SQL 注入", "intentId": intent["id"], "severity": "high",
            "reproducibleSteps": ["在 id 参数里放单引号"],
            "affectedAssetId": asset["id"]})
        data = self.yes(self.client.get("/api/v1/tasks/%s/coverage" % eid))
        self.assertEqual(1, data["summary"]["tested"])
        self.assertEqual(1, data["assets"][0]["findings"])

    def test_unknown_task_is_404(self):
        self.assertEqual(404, self.client.get("/api/v1/tasks/E-nope/coverage")[0])
        self.assertEqual(404, self.client.get("/api/v1/tasks/E-nope/asset-refs")[0])


class TaskAssetTests(_TaskOpsCase):
    """资产的两个写入口：补一条、删一条。

    覆盖度那一块列着资产却补不进去、删不掉，是「看得见摸不着」。这一组钉的是
    「摸得着」之后的三件容易错的事：按身份复用而不是每次新造一条、删父不连坐、
    「删了之后谁指着空处」要报出来。
    """

    def _add(self, eid, *assets, expect=201):
        return self.yes(self.client.post("/api/v1/tasks/%s/assets" % eid,
                                         {"assets": list(assets)}), expect)

    def test_a_hand_written_asset_shows_up_in_coverage(self):
        eid = _task(self.store)
        made = self._add(eid, {"type": "ip", "value": "10.10.10.160"})
        self.assertEqual(1, made["count"])
        self.assertEqual(1, len(made["created"]))
        self.assertEqual(0, len(made["reused"]))
        self.assertEqual("10.10.10.160", made["created"][0]["value"])
        self.assertEqual("IP", made["created"][0]["typeLabel"])

        data = self.yes(self.client.get("/api/v1/tasks/%s/coverage" % eid))
        self.assertEqual(1, data["summary"]["assets"])
        self.assertFalse(data["assets"][0]["tested"])
        self.assertEqual("10.10.10.160", data["assets"][0]["value"])
        # 这一块现在既是读数也是写入口，所以它得自己说「你画不画那颗按钮」。
        self.assertTrue(data["admin"])
        # 词表跟着读数一起回来：界面上的下拉不该另抄一份。
        self.assertEqual(6, len(data["assetTypes"]))
        self.assertEqual("IP", data["assetTypeLabels"]["ip"])

    def test_the_same_identity_comes_back_as_reused_not_a_second_row(self):
        eid = _task(self.store)
        first = self._add(eid, {"type": "ip", "value": "10.10.10.161"})
        again = self._add(eid, {"type": "ip", "value": "10.10.10.161"})
        # count 是「这一批里处理了几条」，不是「新造了几条」——分开放的两拨才是
        # 那句「它是不是已经有了」的答案。
        self.assertEqual(1, again["count"])
        self.assertEqual(0, len(again["created"]))
        self.assertEqual(1, len(again["reused"]))
        self.assertEqual(first["created"][0]["id"], again["reused"][0]["id"])
        data = self.yes(self.client.get("/api/v1/tasks/%s/coverage" % eid))
        self.assertEqual(1, data["summary"]["assets"])

    def test_a_service_is_compared_within_its_parent(self):
        """service 的值形如 `22/tcp ssh`，不含主机。

        只按 (type, value) 认身份的话，第二台机器上的 22 端口会跟第一台合成一条，
        覆盖度里少掉一台。
        """
        eid = _task(self.store)
        made = self._add(eid, {"type": "ip", "value": "10.10.10.162"},
                         {"type": "ip", "value": "10.10.10.163"})
        host_a, host_b = made["created"][0]["id"], made["created"][1]["id"]
        ports = self._add(eid,
                          {"type": "service", "value": "22/tcp ssh",
                           "parentId": host_a},
                          {"type": "service", "value": "22/tcp ssh",
                           "parentId": host_b})
        self.assertEqual(2, len(ports["created"]))
        self.assertNotEqual(ports["created"][0]["id"], ports["created"][1]["id"])
        same = self._add(eid, {"type": "service", "value": "22/tcp ssh",
                               "parentId": host_a})
        self.assertEqual(ports["created"][0]["id"], same["reused"][0]["id"])

    def test_a_missing_parent_and_an_empty_batch_are_400(self):
        eid = _task(self.store)
        status, payload = self.client.post("/api/v1/tasks/%s/assets" % eid,
                                           {"assets": []})
        self.assertEqual(400, status, payload)
        self.assertIn("assets 不能为空", payload["error"])
        status, payload = self.client.post("/api/v1/tasks/%s/assets" % eid,
                                           {"assets": [{"type": "ip",
                                                       "value": "10.10.10.164",
                                                       "parentId": "asset-nope"}]})
        self.assertEqual(400, status, payload)
        self.assertIn("父资产不存在", payload["error"])
        status, payload = self.client.post("/api/v1/tasks/%s/assets" % eid,
                                           {"assets": ["10.10.10.165"]})
        self.assertEqual(400, status, payload)
        self.assertIn("第 1 项不是对象", payload["error"])

    def test_deleting_a_parent_reports_who_now_points_at_nothing(self):
        eid = _task(self.store)
        made = self._add(eid, {"type": "ip", "value": "10.10.10.166"})
        host = made["created"][0]["id"]
        child = self.store.add_node(eid, "asset", {"type": "service",
                                                  "value": "443/tcp https",
                                                  "parentId": host})
        intent = self.store.add_node(eid, "intent", {"title": "打 166",
                                                     "goalId": "goal-1"})
        finding = self.store.add_node(eid, "finding", {
            "title": "未授权访问", "intentId": intent["id"], "severity": "medium",
            "reproducibleSteps": ["直接打开 /admin"],
            "affectedAssetId": host})
        gone = self.yes(self.client.post(
            "/api/v1/tasks/%s/assets/%s/delete" % (eid, host)))
        self.assertEqual(host, gone["deleted"])
        self.assertEqual([child["id"]], gone["children"])
        self.assertEqual([finding["id"]], gone["findingsWithDanglingAsset"])
        # 子资产不连坐：删父通常是「这条记错了」，子资产往往才是真正测过的东西。
        data = self.yes(self.client.get("/api/v1/tasks/%s/coverage" % eid))
        self.assertEqual(1, data["summary"]["assets"])
        self.assertEqual(child["id"], data["assets"][0]["assetId"])
        # 父没了，它被提成根：指向空处比悄悄消失诚实。
        self.assertFalse(data["assets"][0].get("parentId"))

    def test_deleting_something_that_is_not_an_asset_is_404(self):
        eid = _task(self.store)
        status, payload = self.client.post(
            "/api/v1/tasks/%s/assets/asset-nope/delete" % eid)
        self.assertEqual(404, status, payload)
        self.assertIn("资产不存在", payload["error"])
        intent = self.store.add_node(eid, "intent", {"title": "扫一圈",
                                                     "goalId": "goal-1"})
        status, payload = self.client.post(
            "/api/v1/tasks/%s/assets/%s/delete" % (eid, intent["id"]))
        self.assertEqual(404, status, payload)
        self.assertIn("这不是一条资产", payload["error"])

    def test_writing_assets_needs_an_admin(self):
        eid = _task(self.store)
        made = self._add(eid, {"type": "ip", "value": "10.10.10.167"})
        operator = self.operator()
        status, payload = operator.post("/api/v1/tasks/%s/assets" % eid,
                                        {"assets": [{"type": "ip",
                                                    "value": "10.10.10.168"}]})
        self.assertEqual(403, status, payload)
        self.assertIn("管理员", payload["error"])
        status, payload = operator.post(
            "/api/v1/tasks/%s/assets/%s/delete" % (eid, made["created"][0]["id"]))
        self.assertEqual(403, status, payload)
        self.assertIn("管理员", payload["error"])
        # 读得到，只是画不画入口由后端那一句话说。
        data = self.yes(operator.get("/api/v1/tasks/%s/coverage" % eid))
        self.assertEqual(1, data["summary"]["assets"])
        self.assertFalse(data["admin"])
class TaskIntelligenceTests(_TaskOpsCase):
    def test_validation_order_is_kind_then_title_then_content(self):
        eid = _task(self.store)
        status, payload = self.client.post("/api/v1/tasks/%s/intelligence" % eid,
                                           {"title": "x", "content": "y"})
        self.assertEqual(400, status, payload)
        self.assertIn("情报类别", payload["error"])
        status, payload = self.client.post("/api/v1/tasks/%s/intelligence" % eid,
                                           {"kind": "credential"})
        self.assertEqual(400, status, payload)
        self.assertIn("标题不能为空", payload["error"])
        status, payload = self.client.post("/api/v1/tasks/%s/intelligence" % eid,
                                           {"kind": "credential", "title": "admin"})
        self.assertEqual(400, status, payload)
        self.assertIn("内容不能为空", payload["error"])

    def test_one_container_intent_collects_every_note(self):
        eid = _task(self.store)
        made = self.yes(self.client.post("/api/v1/tasks/%s/intelligence" % eid, {
            "kind": "credential", "title": "运维口令",
            "content": "admin / Passw0rd", "evidence": "登录页回显"}), 201)
        self.assertEqual("credential", made["intelligence"]["intelKind"])
        self.yes(self.client.post("/api/v1/tasks/%s/intelligence" % eid, {
            "kind": "lead", "title": "内部 Wiki 可匿名读",
            "content": "信息泄露"}, ), 201)

        data = self.yes(self.client.get("/api/v1/tasks/%s/intelligence" % eid))
        self.assertEqual(2, data["total"])
        self.assertEqual(["lead", "credential"],
                         [row["kind"] for row in data["items"]])
        self.assertEqual("凭据", [row["kindLabel"] for row in data["items"]][1])
        self.assertEqual("凭据", data["kindLabels"]["credential"])
        self.assertEqual(1, len(self.store.list_nodes(eid, "intent")))

    def test_unknown_task_is_404(self):
        status, payload = self.client.post("/api/v1/tasks/E-nope/intelligence",
                                           {"kind": "credential", "title": "t",
                                            "content": "c"})
        self.assertEqual(404, status, payload)

    def _one(self, eid):
        made = self.yes(self.client.post("/api/v1/tasks/%s/intelligence" % eid, {
            "kind": "credential", "title": "运维口令",
            "content": "admin / Passw0rd", "evidence": "登录页回显"}), 201)
        return made["intelligence"]["id"]

    def test_one_note_reads_back_as_the_same_row(self):
        """读一条与列表里的一行**必须同形**：界面上的「改」拿的就是列表那一行，
        两处形状不一致时，改完回填的那一行会少字段。"""
        eid = _task(self.store)
        iid = self._one(eid)
        listed = self.yes(self.client.get("/api/v1/tasks/%s/intelligence" % eid))
        one = self.yes(self.client.get(
            "/api/v1/tasks/%s/intelligence/%s" % (eid, iid)))
        self.assertEqual(listed["items"][0], one["intelligence"])
        self.assertEqual("运维口令", one["intelligence"]["title"])
        self.assertEqual("admin / Passw0rd", one["intelligence"]["content"])
        self.assertEqual("登录页回显", one["intelligence"]["evidence"])

    def test_status_only_update_keeps_the_text(self):
        """只改状态时不碰正文。这一条是上一条的反面：把 detail 无条件拼回去，
        对话里写下的、没有标题的情报（detail 里没有全角冒号）第一次改状态就 400。"""
        eid = _task(self.store)
        iid = self._one(eid)
        changed = self.yes(self.client.post(
            "/api/v1/tasks/%s/intelligence/%s/update" % (eid, iid),
            {"status": "inferred"}))
        self.assertEqual("inferred", changed["intelligence"]["status"])
        self.assertEqual("运维口令", changed["intelligence"]["title"])
        self.assertEqual("admin / Passw0rd", changed["intelligence"]["content"])

    def test_text_update_is_merged_not_replaced(self):
        eid = _task(self.store)
        iid = self._one(eid)
        changed = self.yes(self.client.post(
            "/api/v1/tasks/%s/intelligence/%s/update" % (eid, iid),
            {"title": "运维口令（改）"}))
        self.assertEqual("运维口令（改）", changed["intelligence"]["title"])
        self.assertEqual("admin / Passw0rd", changed["intelligence"]["content"])
        # 空证据是"清掉"，不是"保持原样"
        cleared = self.yes(self.client.post(
            "/api/v1/tasks/%s/intelligence/%s/update" % (eid, iid),
            {"evidence": ""}))
        self.assertEqual("", cleared["intelligence"]["evidence"])

    def test_update_rejects_empty_text_and_unknown_status(self):
        eid = _task(self.store)
        iid = self._one(eid)
        status, payload = self.client.post(
            "/api/v1/tasks/%s/intelligence/%s/update" % (eid, iid),
            {"title": "  "})
        self.assertEqual(400, status, payload)
        self.assertIn("标题不能为空", payload["error"])
        status, payload = self.client.post(
            "/api/v1/tasks/%s/intelligence/%s/update" % (eid, iid),
            {"status": "maybe"})
        self.assertEqual(400, status, payload)
        self.assertIn("情报状态", payload["error"])

    def test_delete_takes_the_edges_with_it(self):
        eid = _task(self.store)
        iid = self._one(eid)
        gone = self.yes(self.client.post(
            "/api/v1/tasks/%s/intelligence/%s/delete" % (eid, iid)))
        self.assertEqual(iid, gone["deleted"])
        self.assertGreaterEqual(gone["edges"], 1)   # 挂在「情报收集」那条意图上
        self.assertEqual(404, self.client.get(
            "/api/v1/tasks/%s/intelligence/%s" % (eid, iid))[0])
        listed = self.yes(self.client.get("/api/v1/tasks/%s/intelligence" % eid))
        self.assertEqual(0, listed["total"])

    def test_reading_something_that_is_not_a_note_says_so(self):
        """三种"找不到"分开：id 写错、这条节点不是事实、这条事实不是情报。"""
        eid = _task(self.store)
        self._one(eid)
        status, payload = self.client.get(
            "/api/v1/tasks/%s/intelligence/fact-nope" % eid)
        self.assertEqual(404, status, payload)
        self.assertIn("情报不存在", payload["error"])
        intent = self.store.list_nodes(eid, "intent")[0]["id"]
        status, payload = self.client.get(
            "/api/v1/tasks/%s/intelligence/%s" % (eid, intent))
        self.assertEqual(404, status, payload)
        self.assertIn("不是一条情报", payload["error"])

    def test_writes_are_admin_only_and_the_list_says_which_side_you_are_on(self):
        """改/删与新增同一档：写进去的东西会进报告的证据面。
        列表额外回一位 admin，界面照它决定画不画入口——猜角色猜错的两个方向都难查。"""
        eid = _task(self.store)
        iid = self._one(eid)
        mine = self.yes(self.client.get("/api/v1/tasks/%s/intelligence" % eid))
        self.assertTrue(mine["admin"])
        other = self.operator()
        theirs = self.yes(other.get("/api/v1/tasks/%s/intelligence" % eid))
        self.assertFalse(theirs["admin"])
        for path, body in (
                ("/api/v1/tasks/%s/intelligence/%s/update" % (eid, iid),
                 {"status": "inferred"}),
                ("/api/v1/tasks/%s/intelligence/%s/delete" % (eid, iid), {})):
            status, payload = other.post(path, body)
            self.assertEqual(403, status, (path, payload))
        # 被拒之后那一条还在
        self.yes(self.client.get("/api/v1/tasks/%s/intelligence/%s" % (eid, iid)))


class TaskModelTests(_TaskOpsCase):
    def test_resolution_says_which_profile_actually_runs(self):
        eid = _task(self.store)
        empty = self.yes(self.client.get("/api/v1/tasks/%s/llm" % eid))
        self.assertEqual("none", empty["source"])
        self.assertIsNone(empty["profile"])

        profile = _profile(self.store, "全局那份")
        self.store.activate_profile(profile["id"])
        fallback = self.yes(self.client.get("/api/v1/tasks/%s/llm" % eid))
        self.assertEqual("global", fallback["source"])
        self.assertEqual(profile["id"], fallback["profileId"])
        self.assertEqual(fallback, self.yes(self.client.get(
            "/api/v1/tasks/%s/llm/resolution" % eid)))

        own = _profile(self.store, "这个任务自己的")
        set_to = self.yes(self.client.post("/api/v1/tasks/%s/llm" % eid,
                                           {"llmProfileId": own["id"]}))
        self.assertEqual("task", set_to["source"])
        self.assertEqual(own["id"], set_to["profileId"])

        self.yes(self.client.post("/api/v1/tasks/%s/llm" % eid,
                                 {"llmProfileId": ""}))
        self.assertEqual("global", self.store.resolve_task_profile(eid)["source"])

    def test_unknown_profile_is_404_not_a_silent_fallback(self):
        eid = _task(self.store)
        status, payload = self.client.post("/api/v1/tasks/%s/llm" % eid,
                                           {"llmProfileId": "m-nope"})
        self.assertEqual(404, status, payload)
        self.assertIn("模型配置不存在", payload["error"])
        self.assertEqual("", self.store.get_task(eid)["llmProfileId"])


class TaskIntentTests(_TaskOpsCase):
    def test_pause_resume_cancel_map_onto_the_state_machine(self):
        eid = _task(self.store)
        intent = self.store.add_node(eid, "intent", {"title": "扫端口",
                                                     "goalId": "goal-1"})
        base = "/api/v1/tasks/%s/intents/%s/control" % (eid, intent["id"])
        paused = self.yes(self.client.post(base, {"action": "pause"}))
        self.assertEqual("pending", paused["intent"]["state"])
        resumed = self.yes(self.client.post(base, {"action": "resume"}))
        self.assertEqual("active", resumed["intent"]["state"])
        cancelled = self.yes(self.client.post(base, {"action": "cancel"}))
        self.assertEqual("exhausted", cancelled["intent"]["state"])

        status, payload = self.client.post(base, {"action": "explode"})
        self.assertEqual(400, status, payload)
        self.assertIn("动作只能是", payload["error"])

    def test_messages_keep_write_order_and_rerun_releases_every_blocked_one(self):
        eid = _task(self.store)
        intent = self.store.add_node(eid, "intent", {"title": "扫端口",
                                                     "goalId": "goal-1"})
        first = self.store.add_node(eid, "fact", {"detail": "22 开",
                                                  "intentId": intent["id"],
                                                  "factKind": "port"})
        second = self.store.add_node(eid, "fact", {"detail": "80 开",
                                                   "intentId": intent["id"],
                                                   "factKind": "port"})
        data = self.yes(self.client.get(
            "/api/v1/tasks/%s/intents/%s/messages" % (eid, intent["id"])))
        self.assertEqual([first["id"], second["id"]],
                         [row["id"] for row in data["messages"]])
        self.assertEqual(404, self.client.get(
            "/api/v1/tasks/%s/intents/intent-99/messages"
            % eid)[0])

        self.store.update_node_state(eid, intent["id"], "blocked")
        released = self.yes(self.client.post("/api/v1/tasks/intents/rerun-blocked",
                                             {"taskId": eid}))
        self.assertEqual(1, released["count"])
        self.assertEqual([intent["id"]], released["intentIds"])
        self.assertEqual("active",
                         self.store.get_node(eid, intent["id"])["state"])

    def test_messages_carry_the_label_and_the_hint_channel(self):
        # 界面把「提示」与「22 开」摆在一起时必须分得开：前者是人写下的指令,
        # 后者是探测产物。通道字段就是这个区分，词表由后端给一份。
        eid = _task(self.store)
        intent = self.store.add_node(eid, "intent", {"title": "扫端口",
                                                     "goalId": "goal-1"})
        self.store.add_node(eid, "fact", {"detail": "22 开",
                                           "intentId": intent["id"],
                                           "factKind": "port"})
        self.store.add_node(eid, "fact", {"detail": "别扫 10.0.0.0/8",
                                           "intentId": intent["id"],
                                           "channel": "hint"})
        data = self.yes(self.client.get(
            "/api/v1/tasks/%s/intents/%s/messages" % (eid, intent["id"])))
        rows = data["messages"]
        self.assertEqual("端口", rows[0]["factKindLabel"])
        self.assertEqual("", rows[0]["channel"])
        # 提示那一条没有 factKind（channel 是另一个维度），标签回落成原值空串。
        self.assertEqual("hint", rows[1]["channel"])
        self.assertEqual("说明", data["kindLabels"]["info"])
        self.assertEqual(6, len(data["kindLabels"]))

    def test_rerun_without_a_task_is_400(self):
        status, payload = self.client.post("/api/v1/tasks/intents/rerun-blocked", {})
        self.assertEqual(400, status, payload)
        self.assertIn("要指定任务", payload["error"])


class TaskInterceptTests(_TaskOpsCase):
    def test_task_view_merges_global_rules_with_its_own(self):
        eid = _task(self.store)
        global_count = len(self.store.list_intercept_rules(""))
        empty = self.yes(self.client.get("/api/v1/tasks/%s/intercept-rules" % eid))
        self.assertEqual(global_count, empty["counts"]["total"])
        self.assertEqual(0, empty["counts"]["task"])
        self.assertTrue(all(rule["scope"] == "global" for rule in empty["rules"]))
        self.assertIn("targets", empty["meta"])

        seeded = self.yes(self.client.post(
            "/api/v1/tasks/%s/intercept-rules" % eid))
        self.assertEqual(global_count, seeded["added"])
        merged = self.yes(self.client.get("/api/v1/tasks/%s/intercept-rules" % eid))
        self.assertEqual(global_count * 2, merged["counts"]["total"])
        self.assertEqual(global_count, merged["counts"]["task"])
        self.assertTrue(all(rule["builtin"] for rule in merged["rules"]))
        # 全局那一组一条都不能少：任务级规则是叠在它之上的，不是替换。
        self.assertEqual(global_count,
                         len([rule for rule in merged["rules"]
                              if rule["scope"] == "global"]))
        # 再播一次不写第二份。
        again = self.yes(self.client.post("/api/v1/tasks/%s/intercept-rules" % eid))
        self.assertEqual(0, again["added"])

    def test_seeding_does_not_touch_the_global_table(self):
        eid = _task(self.store)
        before = len(self.store.list_intercept_rules(""))
        self.yes(self.client.post("/api/v1/tasks/%s/intercept-rules" % eid))
        self.assertEqual(before, len(self.store.list_intercept_rules("")))

    def test_reset_only_restores_the_task_copies(self):
        eid = _task(self.store)
        self.yes(self.client.post("/api/v1/tasks/%s/intercept-rules" % eid))
        mine = self.store.save_intercept_rule({
            "name": "自己加的", "pattern": "task-local-secret",
            "matchType": "literal", "priority": 999, "enabled": True,
            "engagementId": eid})
        reset = self.yes(self.client.post("/api/v1/intercept/reset",
                                          {"engagementId": eid}))
        self.assertTrue(reset["deleted"] > 0)
        self.assertEqual(eid, reset["engagementId"])
        # 恢复内置不动自建的：那是别人一下午的配置，抹掉无法撤销。
        self.assertIsNotNone(self.store.get_intercept_rule(mine["id"]))
        global_after = len(self.store.list_intercept_rules(""))
        self.assertEqual(global_after, len(self.store.list_intercept_rules("")))

    def test_guard_uses_the_task_rules_only_when_it_is_told_which_task(self):
        eid = _task(self.store)
        self.store.save_intercept_rule({
            "name": "这个任务专属", "pattern": "task-only-token",
            "matchType": "literal", "priority": 999, "enabled": True,
            "engagementId": eid})
        plane = types.SimpleNamespace(store=self.store)
        blocked, rule = intercept.guard(plane, "shell",
                                        {"command": "echo task-only-token"}, eid)
        self.assertTrue(blocked)
        self.assertEqual("这个任务专属", rule["name"])
        allowed, none = intercept.guard(plane, "shell",
                                        {"command": "echo task-only-token"})
        self.assertEqual("", allowed)
        self.assertIsNone(none)

    def test_global_rules_still_apply_inside_a_task(self):
        eid = _task(self.store)
        self.store.save_intercept_rule({
            "name": "全局那条", "pattern": "global-only-token",
            "matchType": "literal", "priority": 999, "enabled": True})
        plane = types.SimpleNamespace(store=self.store)
        blocked, rule = intercept.guard(plane, "shell",
                                        {"command": "echo global-only-token"}, eid)
        self.assertTrue(blocked)
        self.assertEqual("全局那条", rule["name"])
        listed = self.yes(self.client.get("/api/v1/tasks/%s/intercept-rules" % eid))
        names = [row["name"] for row in listed["rules"]]
        self.assertIn("全局那条", names)

    def test_the_switch_off_lets_everything_through(self):
        eid = _task(self.store)
        self.store.save_intercept_rule({
            "name": "全局那条", "pattern": "global-only-token",
            "matchType": "literal", "priority": 999, "enabled": True})
        self.yes(self.client.post("/api/v1/intercept/settings", {"enabled": False}))
        plane = types.SimpleNamespace(store=self.store)
        allowed, _ = intercept.guard(plane, "shell",
                                     {"command": "echo global-only-token"}, eid)
        self.assertEqual("", allowed)
        listed = self.yes(self.client.get("/api/v1/tasks/%s/intercept-rules" % eid))
        self.assertFalse(listed["enabled"])

    def _rule(self, engagement_id="", name="自己加的", pattern="task-local-secret"):
        # priority 走 1-100 的合法区间：store 直存不校验，但一经 HTTP 改就会
        # 被 normalize_rule 拿 existing 的值重判一遍，超出区间的存量值会当场 400。
        return self.store.save_intercept_rule({
            "name": name, "pattern": pattern, "matchType": "literal",
            "priority": 90, "enabled": True, "engagementId": engagement_id})

    def test_updating_one_rule_keeps_it_where_it_was(self):
        eid = _task(self.store)
        rule = self._rule(eid)
        changed = self.yes(self.client.post(
            "/api/v1/tasks/%s/intercept-rules/%s/update" % (eid, rule["id"]),
            {"pattern": "task-local-secret-v2", "priority": 20}))
        self.assertEqual("task-local-secret-v2", changed["rule"]["pattern"])
        self.assertEqual(20, changed["rule"]["priority"])
        # 只发想改的那一项，其余照旧：整条更新要求把 name 也带上，那是另一件事。
        self.assertEqual("自己加的", changed["rule"]["name"])
        self.assertEqual(eid, changed["rule"]["engagementId"])

    def test_updating_a_global_rule_from_a_task_keeps_it_global(self):
        eid = _task(self.store)
        rule = self._rule("")
        changed = self.yes(self.client.post(
            "/api/v1/tasks/%s/intercept-rules/%s/update" % (eid, rule["id"]),
            {"pattern": "global-now-matches-this", "engagementId": eid}))
        # 这一层不改可见范围：把一条全局规则「顺手」收成本任务私有，等于在一条任务的
        # 页面里让别的任务不再被它拦，而屏幕上什么都没变。
        self.assertEqual("", changed["rule"]["engagementId"])
        self.assertIn(rule["id"],
                      [row["id"] for row in self.store.list_intercept_rules("")])

    def test_a_rule_of_another_task_is_not_found_not_forbidden(self):
        eid = _task(self.store)
        other = _task(self.store, "10.10.10.111")
        theirs = self._rule(other, name="隔壁的", pattern="neighbour-only")
        for suffix, body in (("update", {"pattern": "x"}), ("toggle", {}),
                             ("delete", {})):
            status, payload = self.client.post(
                "/api/v1/tasks/%s/intercept-rules/%s/%s" % (eid, theirs["id"], suffix),
                body)
            self.assertEqual(404, status, (suffix, payload))
            self.assertIn("不属于这条任务", payload["error"])
        # 一条都没被动过。
        self.assertIsNotNone(self.store.get_intercept_rule(theirs["id"]))

    def test_toggle_flips_when_it_is_not_told_which_way(self):
        eid = _task(self.store)
        rule = self._rule(eid)
        off = self.yes(self.client.post(
            "/api/v1/tasks/%s/intercept-rules/%s/toggle" % (eid, rule["id"]), {}))
        self.assertTrue(off["changed"])
        self.assertFalse(off["rule"]["enabled"])
        # 已经是这个状态就不再写一次，回 changed False：界面上那颗按钮的文案才不会闪。
        same = self.yes(self.client.post(
            "/api/v1/tasks/%s/intercept-rules/%s/toggle" % (eid, rule["id"]),
            {"enabled": False}))
        self.assertFalse(same["changed"])
        back = self.yes(self.client.post(
            "/api/v1/tasks/%s/intercept-rules/%s/toggle" % (eid, rule["id"]),
            {"enabled": True}))
        self.assertTrue(back["rule"]["enabled"])

    def test_delete_removes_it_from_the_merged_view(self):
        eid = _task(self.store)
        rule = self._rule(eid)
        gone = self.yes(self.client.post(
            "/api/v1/tasks/%s/intercept-rules/%s/delete" % (eid, rule["id"])))
        self.assertEqual(rule["id"], gone["deleted"])
        listed = self.yes(self.client.get("/api/v1/tasks/%s/intercept-rules" % eid))
        self.assertNotIn(rule["id"], [row["id"] for row in listed["rules"]])
        # 删掉的第二条路：再删一次就是 404，不是静默成功。
        status, payload = self.client.post(
            "/api/v1/tasks/%s/intercept-rules/%s/delete" % (eid, rule["id"]))
        self.assertEqual(404, status, payload)

    def test_writing_a_rule_through_a_task_needs_an_admin(self):
        eid = _task(self.store)
        rule = self._rule(eid)
        operator = self.operator()
        for suffix, body in (("update", {"pattern": "x"}), ("toggle", {}),
                             ("delete", {})):
            status, payload = operator.post(
                "/api/v1/tasks/%s/intercept-rules/%s/%s" % (eid, rule["id"], suffix),
                body)
            self.assertEqual(403, status, (suffix, payload))
            self.assertIn("管理员", payload["error"])
        self.assertIsNotNone(self.store.get_intercept_rule(rule["id"]))
class TaskGoalTests(_TaskOpsCase):
    """任务详情里的目标清单。

    目标是**报告第一行读的那句话**，也是规划器派活的依据。所以这一组钉三件事：
    加一条排在后面（不占 goal-1）、改正文是合并语义（只发想改的那一项，说明不会被
    冲掉）、删一条会带走它的关系边而 goal-1 一个字节都不能动。
    """

    def test_the_built_goal_comes_first_and_new_ones_queue_behind_it(self):
        eid = _task(self.store)
        listed = self.yes(self.client.get("/api/v1/tasks/%s/goals" % eid))
        self.assertEqual(["goal-1"], [g["id"] for g in listed["goals"]])
        # goal-1 的正文就是建档时那句被测目标（`create_engagement` 的第一个参数）。
        self.assertEqual("10.10.10.110", listed["goals"][0]["text"])

        made = self.yes(self.client.post("/api/v1/tasks/%s/goals" % eid,
                                        {"text": "拿到数据库"}), 201)
        self.assertEqual("goal-2", made["goal"]["id"])
        self.assertEqual(eid, made["taskId"])
        listed = self.yes(self.client.get("/api/v1/tasks/%s/goals" % eid))
        self.assertEqual(["goal-1", "goal-2"], [g["id"] for g in listed["goals"]])
        self.assertEqual(2, listed["counts"]["goals"])

    def test_an_empty_goal_is_rejected(self):
        eid = _task(self.store)
        status, payload = self.client.post("/api/v1/tasks/%s/goals" % eid,
                                          {"text": "   "})
        self.assertEqual(400, status, payload)

    def test_editing_merges_and_keeps_what_was_not_sent(self):
        eid = _task(self.store)
        made = self.yes(self.client.post("/api/v1/tasks/%s/goals" % eid,
                                        {"text": "拿到数据库", "objective": "拖库前先验证"}), 201)
        goal_id = made["goal"]["id"]
        changed = self.yes(self.client.post(
            "/api/v1/tasks/%s/goals/%s/update" % (eid, goal_id),
            {"text": "拿到只读账号"}))
        self.assertTrue(changed["changed"])
        # 只发了正文，说明不能跟着被清成空串——合并语义，不是整条覆盖。
        self.assertEqual("拿到只读账号", changed["goal"]["text"])
        self.assertEqual("拖库前先验证", changed["goal"]["objective"])

    def test_an_edit_with_nothing_to_change_does_not_touch_the_row(self):
        eid = _task(self.store)
        goal_id = self.yes(self.client.post("/api/v1/tasks/%s/goals" % eid,
                                           {"text": "拿到数据库"}), 201)["goal"]["id"]
        same = self.yes(self.client.post(
            "/api/v1/tasks/%s/goals/%s/update" % (eid, goal_id), {}))
        # 一个键都没给就不写库：写一次等于推一次 updated_at，而「刚刚改过」
        # 在任务列表里是排序依据。
        self.assertFalse(same["changed"])
        self.assertEqual("拿到数据库", same["goal"]["text"])

    def test_deleting_takes_the_spawn_edge_with_it(self):
        eid = _task(self.store)
        goal_id = self.yes(self.client.post("/api/v1/tasks/%s/goals" % eid,
                                           {"text": "拿到数据库"}), 201)["goal"]["id"]
        self.yes(self.client.post("/api/v1/engagements/%s/intents" % eid,
                                  {"title": "枚举 /api", "goalId": goal_id}), 201)
        gone = self.yes(self.client.post(
            "/api/v1/tasks/%s/goals/%s/delete" % (eid, goal_id)))
        self.assertEqual(goal_id, gone["deleted"])
        self.assertGreaterEqual(gone["edges"], 1)
        listed = self.yes(self.client.get("/api/v1/tasks/%s/goals" % eid))
        self.assertEqual(["goal-1"], [g["id"] for g in listed["goals"]])

    def test_the_built_goal_cannot_be_deleted(self):
        eid = _task(self.store)
        status, payload = self.client.post(
            "/api/v1/tasks/%s/goals/goal-1/delete" % eid)
        # 它是建档目标，被 engine 的 GOAL_ID 硬引用；删了之后那条引用的落点是空。
        self.assertEqual(400, status, payload)
        self.assertIn("goal-1", payload["error"])

    def test_reading_something_that_is_not_a_goal_says_so(self):
        eid = _task(self.store)
        status, payload = self.client.get("/api/v1/tasks/%s/goals/goal-nope" % eid)
        self.assertEqual(404, status, payload)
        self.assertIn("目标不存在", payload["error"])
        intent = self.yes(self.client.post("/api/v1/engagements/%s/intents" % eid,
                                          {"title": "枚举", "goalId": "goal-1"}), 201)
        status, payload = self.client.get(
            "/api/v1/tasks/%s/goals/%s" % (eid, intent["intent"]["id"]))
        self.assertEqual(404, status, payload)
        self.assertIn("不是一个目标", payload["error"])

    def test_unknown_task_is_404(self):
        self.assertEqual(404, self.client.get("/api/v1/tasks/E-nope/goals")[0])

    def test_writes_need_an_admin_and_the_block_says_which_side_you_are_on(self):
        eid = _task(self.store)
        goal_id = self.yes(self.client.post("/api/v1/tasks/%s/goals" % eid,
                                           {"text": "拿到数据库"}), 201)["goal"]["id"]
        admin_view = self.yes(self.client.get("/api/v1/tasks/%s/goals" % eid))
        self.assertTrue(admin_view["admin"])
        operator = self.operator()
        for path, body in (("/goals", {"text": "x"}),
                           ("/goals/%s/update" % goal_id, {"text": "x"}),
                           ("/goals/%s/delete" % goal_id, {})):
            status, payload = operator.post("/api/v1/tasks/%s%s" % (eid, path), body)
            self.assertEqual(403, status, (path, payload))
            self.assertIn("管理员", payload["error"])
        # 读只要登录，且同样带着那一位（界面靠它决定画不画改/删）。
        seen = self.yes(operator.get("/api/v1/tasks/%s/goals" % eid))
        self.assertFalse(seen["admin"])
        self.assertEqual(2, len(seen["goals"]))


class TaskScopeTests(_TaskOpsCase):
    """授权范围的两条路：整屏替换与单条增删。

    它们是**并存**的，不是新旧替换：整屏保存解决「这一屏就是全部」，单条增删解决
    「我刚加错一条」。两个人同时在一个页面上改，整屏保存会把对方刚加的条目一起
    冲掉——而屏幕上没有任何东西提示这件事发生过。
    """

    def test_one_entry_can_be_added_and_read_back(self):
        eid = _task(self.store)
        made = self.yes(self.client.post("/api/v1/tasks/%s/constraints" % eid,
                                        {"kind": "deny", "text": "10.10.10.0/24"}), 201)
        self.assertEqual("deny", made["entry"]["kind"])
        self.assertEqual("10.10.10.0/24", made["entry"]["text"])
        listed = self.yes(self.client.get("/api/v1/tasks/%s/scope" % eid))
        self.assertEqual(1, listed["counts"]["deny"])
        self.assertTrue(listed["admin"])

    def test_a_blank_entry_and_a_bad_kind_are_rejected(self):
        eid = _task(self.store)
        status, payload = self.client.post("/api/v1/tasks/%s/constraints" % eid,
                                          {"kind": "allow", "text": "   "})
        self.assertEqual(400, status, payload)
        status, payload = self.client.post("/api/v1/tasks/%s/constraints" % eid,
                                          {"kind": "maybe", "text": "x"})
        self.assertEqual(400, status, payload)
        self.assertIn("约束类型", payload["error"])

    def test_deleting_one_entry_leaves_the_rest_alone(self):
        eid = _task(self.store)
        keep = self.yes(self.client.post("/api/v1/tasks/%s/constraints" % eid,
                                        {"kind": "allow", "text": "10.0.0.1"}), 201)
        drop = self.yes(self.client.post("/api/v1/tasks/%s/constraints" % eid,
                                        {"kind": "deny", "text": "10.0.0.2"}), 201)
        gone = self.yes(self.client.post(
            "/api/v1/tasks/%s/constraints/%s/delete" % (eid, drop["entry"]["id"])))
        self.assertEqual(drop["entry"]["id"], gone["deleted"])
        self.assertEqual([keep["entry"]["id"]],
                         [row["id"] for row in gone["entries"]])

    def test_deleting_what_is_not_there_is_404(self):
        eid = _task(self.store)
        self.assertEqual(404, self.client.post(
            "/api/v1/tasks/%s/constraints/ec-nope/delete" % eid)[0])

    def test_an_entry_of_another_task_is_not_reachable_through_this_one(self):
        one = _task(self.store)
        other = self.store.create_engagement("10.10.10.111", "另一单", "本人授权")["id"]
        mine = self.yes(self.client.post("/api/v1/tasks/%s/constraints" % other,
                                        {"kind": "allow", "text": "10.0.0.9"}), 201)
        # 归属在 SQL 的 WHERE 里一起判（engagement_id=? AND id=?），
        # 所以拿别条任务的 id 来删，是「这条任务名下没有这条」。
        self.assertEqual(404, self.client.post(
            "/api/v1/tasks/%s/constraints/%s/delete"
            % (one, mine["entry"]["id"]))[0])

    def test_writing_a_scope_entry_needs_an_admin(self):
        eid = _task(self.store)
        made = self.yes(self.client.post("/api/v1/tasks/%s/constraints" % eid,
                                        {"kind": "allow", "text": "10.0.0.1"}), 201)
        operator = self.operator()
        for path, body in (("/constraints", {"kind": "allow", "text": "x"}),
                           ("/constraints/%s/delete" % made["entry"]["id"], {})):
            status, payload = operator.post("/api/v1/tasks/%s%s" % (eid, path), body)
            self.assertEqual(403, status, (path, payload))
            self.assertIn("管理员", payload["error"])
        seen = self.yes(operator.get("/api/v1/tasks/%s/scope" % eid))
        self.assertFalse(seen["admin"])



class TaskRetestTests(_TaskOpsCase):
    """漏洞复测：写一档结论、历史只增不改、只有「已修复」动状态位。

    这一组钉的是与对话里那条 `record_finding_retest_result` 的**同一份语义**：
    界面点一次、模型写一次，读回来必须是同一种行。两边各写一套的代价是
    「界面上能存的三档结论，模型写不进去」或者反过来——而那种差异在页面上
    完全看不出来。
    """

    def _finding(self, eid, title="SQL 注入", severity="high"):
        intent = self.store.add_node(eid, "intent", {"title": "打 portal",
                                                    "goalId": "goal-1"})
        return self.store.add_node(eid, "finding", {
            "title": title, "intentId": intent["id"], "severity": severity,
            "reproducibleSteps": ["在 id 参数里放单引号"]})

    def _retest(self, eid, fid, verdict, summary="还能登", evidence="又登了一次"):
        return self.client.post("/api/v1/tasks/%s/findings/%s/retest" % (eid, fid),
                                {"verdict": verdict, "summary": summary,
                                 "evidence": evidence})

    def test_every_retest_is_appended_and_the_history_stays(self):
        eid = _task(self.store)
        fid = self._finding(eid)["id"]
        first = self.yes(self._retest(eid, fid, "reproduced"), 201)
        self.assertEqual(1, first["count"])
        self.assertEqual("reproduced", first["verdict"])
        second = self.yes(self._retest(eid, fid, "inconclusive", "看不清",
                                       "环境被关掉了"), 201)
        self.assertEqual(2, second["count"], "复测记录是追加，不是覆盖")
        self.assertEqual(["reproduced", "inconclusive"],
                         [row["verdict"] for row in second["retests"]])
        self.assertTrue(all(row.get("at") for row in second["retests"]),
                        "每一条都要留下时间：没有时间的历史无法与报告对应")

    def test_only_the_fixed_verdict_moves_the_state(self):
        eid = _task(self.store)
        fid = self._finding(eid)["id"]
        kept = self.yes(self._retest(eid, fid, "reproduced"), 201)
        self.assertFalse(kept["stateChanged"])
        self.assertEqual("confirmed", kept["finding"]["state"])

        fixed = self.yes(self._retest(eid, fid, "fixed", "改了密码", "旧口令 401"), 201)
        self.assertTrue(fixed["stateChanged"], "结论是「已修复」时状态要跟着走")
        self.assertEqual("fixed", fixed["finding"]["state"])
        # 状态位是给过滤器与统计用的：只记正文不动它的话，按状态筛「还开着的」
        # 会把已经修好的漏洞一起列出来。
        listed = self.yes(self.client.get("/api/v1/tasks/%s/findings" % eid))
        self.assertEqual("fixed", listed["findings"][0]["state"])

        again = self.yes(self._retest(eid, fid, "reproduced", "又出现了",
                                      "同一个口令又能登"), 201)
        self.assertFalse(again["stateChanged"],
                         "已经修好的漏洞又复现时，状态不该被这条记录悄悄改回去；"
                         "「修了又复发」要看历史，不是靠状态位")
        self.assertEqual("fixed", again["finding"]["state"])

    def test_a_bad_verdict_and_missing_evidence_are_400(self):
        eid = _task(self.store)
        fid = self._finding(eid)["id"]
        status, payload = self.client.post(
            "/api/v1/tasks/%s/findings/%s/retest" % (eid, fid),
            {"verdict": "maybe", "summary": "说不清", "evidence": "x"})
        self.assertEqual(400, status, payload)
        self.assertIn("复测结论", payload.get("error", ""))

        status, payload = self.client.post(
            "/api/v1/tasks/%s/findings/%s/retest" % (eid, fid),
            {"verdict": "fixed", "summary": "改了"})
        self.assertEqual(400, status, payload)
        self.assertIn("evidence", payload.get("error", ""))
        # 没有留下半条记录：校验在任何写之前。
        listed = self.yes(self.client.get("/api/v1/tasks/%s/findings" % eid))
        self.assertFalse(listed["findings"][0].get("retests"))

    def test_a_finding_of_another_task_is_not_reachable_through_this_one(self):
        mine = _task(self.store)
        theirs = _task(self.store, "10.10.10.111")
        fid = self._finding(theirs)["id"]
        status, payload = self._retest(mine, fid, "fixed")
        self.assertEqual(404, status, payload)
        self.assertIn("这个漏洞", payload.get("error", ""))
        # 归属判定在前：那条漏洞本身没被动过。
        node = self.store.get_node(theirs, fid)
        self.assertFalse((node.get("payload") or {}).get("retests"))

    def test_unknown_task_and_an_intent_are_not_findings(self):
        eid = _task(self.store)
        self.assertEqual(404, self._retest("E-nope", "finding-1", "fixed")[0])
        intent = self.store.add_node(eid, "intent", {"title": "打 portal",
                                                    "goalId": "goal-1"})
        self.assertEqual(404, self._retest(eid, intent["id"], "fixed")[0])

    def test_the_list_carries_the_vocabulary_and_the_counts(self):
        eid = _task(self.store)
        fid = self._finding(eid)["id"]
        self._retest(eid, fid, "reproduced")
        data = self.yes(self.client.get("/api/v1/tasks/%s/findings" % eid))
        self.assertEqual(["reproduced", "fixed", "inconclusive"],
                         data["retestVerdicts"])
        self.assertEqual("已修复", data["retestLabels"]["fixed"])
        self.assertEqual(1, data["counts"]["total"])
        self.assertEqual(1, data["counts"]["retested"])
        self.assertEqual(1, len(data["groups"]), "分组与清单来自同一次查询")
        self.assertTrue(data["admin"])

    def test_writing_a_retest_needs_an_admin(self):
        eid = _task(self.store)
        fid = self._finding(eid)["id"]
        peer = self.operator()
        listing = self.yes(peer.get("/api/v1/tasks/%s/findings" % eid))
        self.assertFalse(listing["admin"], "操作员读得到清单，但看不到改的入口")
        status, payload = peer.post(
            "/api/v1/tasks/%s/findings/%s/retest" % (eid, fid),
            {"verdict": "fixed", "summary": "改了", "evidence": "旧口令 401"})
        self.assertEqual(403, status, payload)
        self.assertIn("管理员", payload.get("error", ""))


class TaskOpsPermissionTests(unittest.TestCase):
    def test_anonymous_sees_nothing(self):
        directory, store = support.temporary_store("task-ops-anon.db")
        eid = _task(store)
        httpd, client = support.start(store, admin=False)
        try:
            for path in ("/api/v1/task-categories", "/api/v1/task-templates",
                         "/api/v1/tasks/archive/summary",
                         "/api/v1/tasks/%s/scope" % eid,
                         "/api/v1/tasks/%s/coverage" % eid,
                         "/api/v1/tasks/%s/intelligence" % eid,
                         "/api/v1/tasks/%s/llm" % eid,
                         "/api/v1/tasks/%s/asset-refs" % eid,
                         "/api/v1/tasks/%s/intercept-rules" % eid):
                status, payload = client.get(path)
                self.assertEqual(401, status, (path, payload))
            status, payload = client.post("/api/v1/tasks/create",
                                          {"target": "10.10.10.9"})
            self.assertEqual(401, status, payload)
        finally:
            support.stop(httpd, store, directory)

    def test_operator_may_read_but_never_write(self):
        directory, store = support.temporary_store("task-ops-operator.db")
        eid = _task(store)
        httpd, client = support.start(store)
        try:
            store.create_user("operator", auth.hash_password("Operator-Pass1!"),
                              role="operator")
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            operator = support.Client(base)
            status, payload = operator.login("operator", "Operator-Pass1!")
            self.assertEqual(200, status, payload)

            status, payload = operator.get("/api/v1/tasks/%s/coverage" % eid)
            self.assertEqual(200, status, payload)
            status, payload = operator.get("/api/v1/task-categories")
            self.assertEqual(200, status, payload)

            for path, body in (("/api/v1/tasks/create", {"target": "10.10.10.9"}),
                               ("/api/v1/task-categories", {"name": "红队"}),
                               ("/api/v1/tasks/%s/scope" % eid,
                                {"entries": []}),
                               ("/api/v1/tasks/%s/intelligence" % eid,
                                {"kind": "lead", "title": "t", "content": "c"}),
                               ("/api/v1/tasks/%s/intercept-rules" % eid, {}),
                               ("/api/v1/tasks/control/batch",
                                {"taskIds": [eid], "action": "pause"})):
                status, payload = operator.post(path, body)
                self.assertEqual(403, status, (path, payload))
                self.assertIn("管理员", payload["error"])
        finally:
            support.stop(httpd, store, directory)


if __name__ == "__main__":
    unittest.main()
